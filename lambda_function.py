import os
import time
import random
import boto3
from datetime import datetime, timedelta, timezone
from botocore.exceptions import ClientError

ec2 = boto3.client("ec2")

TAG_KEY = os.environ.get("INSTANCE_TAG_KEY", "Backup")
TAG_VALUE = os.environ.get("INSTANCE_TAG_VALUE", "Daily")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))

CREATED_BY_KEY = "CreatedBy"
CREATED_BY_VAL = "AutoBackupLambda"
PROJECT_KEY = "Project"
PROJECT_VAL = "Automated-DR-EC2-Backup"


def utc_now():
    return datetime.now(timezone.utc)


def cutoff_time():
    return utc_now() - timedelta(days=RETENTION_DAYS)


def call_with_backoff(fn, **kwargs):
    """
    Exponential backoff with jitter for AWS API throttling.
    Handles SnapshotCreationPerVolumeRateExceeded and common throttling errors.
    """
    max_attempts = 7
    base = 1.0  # seconds

    for attempt in range(1, max_attempts + 1):
        try:
            return fn(**kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in ["SnapshotCreationPerVolumeRateExceeded", "RequestLimitExceeded", "Throttling"]:
                sleep_s = (base * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
                print(f"Throttled ({code}). Sleeping {sleep_s:.2f}s then retrying (attempt {attempt}/{max_attempts})...")
                time.sleep(sleep_s)
                continue
            raise


def get_instances_by_tag():
    resp = ec2.describe_instances(
        Filters=[
            {"Name": f"tag:{TAG_KEY}", "Values": [TAG_VALUE]},
            {"Name": "instance-state-name", "Values": ["running", "stopped"]},
        ]
    )
    instances = []
    for r in resp["Reservations"]:
        for i in r["Instances"]:
            instances.append(i)
    return instances


def tag_resource(resource_id, extra_tags=None):
    tags = [
        {"Key": CREATED_BY_KEY, "Value": CREATED_BY_VAL},
        {"Key": PROJECT_KEY, "Value": PROJECT_VAL},
        {"Key": "RetentionDays", "Value": str(RETENTION_DAYS)},
    ]
    if extra_tags:
        for k, v in extra_tags.items():
            tags.append({"Key": k, "Value": str(v)})

    call_with_backoff(ec2.create_tags, Resources=[resource_id], Tags=tags)


def create_volume_snapshots(instance):
    instance_id = instance["InstanceId"]
    snapshots = []

    for mapping in instance.get("BlockDeviceMappings", []):
        ebs = mapping.get("Ebs")
        if not ebs or "VolumeId" not in ebs:
            continue

        volume_id = ebs["VolumeId"]
        desc = f"AutoBackup snapshot for {instance_id} volume {volume_id} ({TAG_KEY}={TAG_VALUE})"

        snap = call_with_backoff(ec2.create_snapshot, VolumeId=volume_id, Description=desc)
        snap_id = snap["SnapshotId"]

        tag_resource(
            snap_id,
            extra_tags={"InstanceId": instance_id, "VolumeId": volume_id, "Type": "EBS-Snapshot"}
        )
        snapshots.append(snap_id)

    return snapshots


def create_ami(instance):
    instance_id = instance["InstanceId"]
    ts = utc_now().strftime("%Y-%m-%d-%H%M%S")
    name = f"autobackup-{instance_id}-{ts}"
    desc = f"AutoBackup AMI for {instance_id} ({TAG_KEY}={TAG_VALUE})"

    resp = call_with_backoff(
        ec2.create_image,
        InstanceId=instance_id,
        Name=name,
        Description=desc,
        NoReboot=True
    )
    image_id = resp["ImageId"]

    tag_resource(image_id, extra_tags={"InstanceId": instance_id, "Type": "AMI"})
    return image_id


def cleanup_old_snapshots():
    # Only delete snapshots created by this Lambda to avoid accidents.
    resp = ec2.describe_snapshots(
        OwnerIds=["self"],
        Filters=[{"Name": f"tag:{CREATED_BY_KEY}", "Values": [CREATED_BY_VAL]}]
    )

    deleted = []
    for s in resp.get("Snapshots", []):
        if s["StartTime"] < cutoff_time():
            snap_id = s["SnapshotId"]
            try:
                call_with_backoff(ec2.delete_snapshot, SnapshotId=snap_id)
                deleted.append(snap_id)
            except Exception as e:
                print(f"Could not delete snapshot {snap_id}: {e}")
    return deleted


def cleanup_old_amis():
    # Deregister old AMIs created by this Lambda and delete their backing snapshots.
    resp = ec2.describe_images(
        Owners=["self"],
        Filters=[{"Name": f"tag:{CREATED_BY_KEY}", "Values": [CREATED_BY_VAL]}]
    )

    deleted_amis = []
    deleted_snapshots = []

    for img in resp.get("Images", []):
        created = datetime.fromisoformat(img["CreationDate"].replace("Z", "+00:00"))
        if created >= cutoff_time():
            continue

        image_id = img["ImageId"]

        # Collect snapshots used by the AMI so we can delete them after deregister
        ami_snapshot_ids = []
        for bdm in img.get("BlockDeviceMappings", []):
            ebs = bdm.get("Ebs")
            if ebs and "SnapshotId" in ebs:
                ami_snapshot_ids.append(ebs["SnapshotId"])

        try:
            call_with_backoff(ec2.deregister_image, ImageId=image_id)
            deleted_amis.append(image_id)
        except Exception as e:
            print(f"Could not deregister AMI {image_id}: {e}")
            continue

        # Delete backing snapshots
        for sid in ami_snapshot_ids:
            try:
                call_with_backoff(ec2.delete_snapshot, SnapshotId=sid)
                deleted_snapshots.append(sid)
            except Exception as e:
                print(f"Could not delete AMI snapshot {sid}: {e}")

    return deleted_amis, deleted_snapshots


def lambda_handler(event, context):
    print(f"Starting backup job. Target tag: {TAG_KEY}={TAG_VALUE}, Retention: {RETENTION_DAYS} days")

    instances = get_instances_by_tag()
    print(f"Found {len(instances)} instance(s) to backup.")

    created = {"snapshots": [], "amis": []}

    for inst in instances:
        iid = inst["InstanceId"]
        print(f"Backing up instance: {iid}")

        snaps = create_volume_snapshots(inst)
        print(f"Created snapshots for {iid}: {snaps}")
        created["snapshots"].extend(snaps)

        ami_id = create_ami(inst)
        print(f"Created AMI for {iid}: {ami_id}")
        created["amis"].append(ami_id)

    deleted_snaps = cleanup_old_snapshots()
    print(f"Deleted old snapshots: {deleted_snaps}")

    deleted_amis, deleted_ami_snaps = cleanup_old_amis()
    print(f"Deleted old AMIs: {deleted_amis}")
    print(f"Deleted old AMI snapshots: {deleted_ami_snaps}")

    return {
        "status": "OK",
        "created": created,
        "deleted": {
            "snapshots": deleted_snaps,
            "amis": deleted_amis,
            "ami_snapshots": deleted_ami_snaps
        }
    }