import csv
import json
import time
import boto3
from botocore.exceptions import ClientError

# ================= CONFIGURATION =================
TARGET_ACCOUNTS = [
    "X17587549763",
    "X46639202286",
]
ROLE_NAME = "OrganizationAccountAccessRole" 
OUTPUT_CSV = "ec2_multi_account_ssm_mounts_report.csv"
# =================================================


def assume_role(account_id, role_name):
    """Assumes an IAM role in the target account and returns a Boto3 session."""
    sts_client = boto3.client("sts")
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    try:
        response = sts_client.assume_role(
            RoleArn=role_arn, RoleSessionName="EC2SSMDiskInventorySession"
        )
        creds = response["Credentials"]
        return boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    except ClientError as e:
        print(f"[ERROR] Could not assume role in account {account_id}: {e}")
        return None


def get_enabled_regions(session):
    """Fetches all enabled AWS regions for the account."""
    ec2 = session.client("ec2", region_name="us-east-1")
    regions = ec2.describe_regions(
        Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}]
    )
    return [r["RegionName"] for r in regions["Regions"]]


def parse_ssm_disk_output(raw_output):
    """
    Parses JSON output from SSM containing per-mount usage.
    Returns tuple: (total_used_gb, formatted_mount_breakdown_string)
    """
    try:
        mounts = json.loads(raw_output)
        if not mounts:
            return 0.0, "No local mounts detected"

        total_used = sum(float(m["used_gb"]) for m in mounts)
        breakdown_parts = []
        for m in mounts:
            mount_pt = m["mount"]
            used = float(m["used_gb"])
            total = float(m["total_gb"])
            pct = round((used / total * 100), 1) if total > 0 else 0.0
            breakdown_parts.append(f"{mount_pt}: {used}/{total}GB ({pct}%)")

        return round(total_used, 2), " | ".join(breakdown_parts)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        return "Parse Error", f"Raw output: {raw_output[:100]} ({e})"


def get_ssm_disk_usage_batch(ssm_client, instances):
    """
    Executes OS commands via SSM Run Command across online Linux and Windows instances
    to collect per-mount and total used disk space in GB.
    Returns a dict mapping {instance_id: (total_used_gb, mount_details_str)}.
    """
    results = {}
    if not instances:
        return results

    online_linux = []
    online_windows = []

    paginator = ssm_client.get_paginator("describe_instance_information")
    ssm_info = {}
    for page in paginator.paginate():
        for info in page["InstanceInformationList"]:
            ssm_info[info["InstanceId"]] = info

    for inst in instances:
        iid = inst["InstanceId"]
        if iid not in ssm_info or ssm_info[iid].get("PingStatus") != "Online":
            results[iid] = ("N/A (SSM Offline/Unregistered)", "N/A")
        else:
            platform = ssm_info[iid].get("PlatformType", "Linux")
            if platform == "Windows":
                online_windows.append(iid)
            else:
                online_linux.append(iid)

    def run_ssm_batch(instance_ids, document_name, commands):
        for i in range(0, len(instance_ids), 50):
            batch = instance_ids[i : i + 50]
            try:
                resp = ssm_client.send_command(
                    InstanceIds=batch,
                    DocumentName=document_name,
                    Parameters={"commands": commands},
                    TimeoutSeconds=60,
                )
                command_id = resp["Command"]["CommandId"]
                time.sleep(3)

                for iid in batch:
                    for _ in range(15):
                        try:
                            inv = ssm_client.get_command_invocation(
                                CommandId=command_id, InstanceId=iid
                            )
                            status = inv["Status"]
                            if status in ["Success", "Failed", "TimedOut", "Cancelled"]:
                                if status == "Success":
                                    output = inv["StandardOutputContent"].strip()
                                    results[iid] = parse_ssm_disk_output(output)
                                else:
                                    results[iid] = (f"SSM Error ({status})", "N/A")
                                break
                        except ssm_client.exceptions.InvocationDoesNotExist:
                            pass
                        time.sleep(2)
                    else:
                        results[iid] = ("SSM Timeout", "N/A")

            except ClientError as e:
                err_code = e.response["Error"]["Code"]
                for iid in batch:
                    results[iid] = (f"SSM API Error: {err_code}", "N/A")

    # 1. Linux: Output all real mounted filesystems as JSON (using -P for POSIX single-line output)
    if online_linux:
        linux_cmd = [
            """df -kP -x tmpfs -x devtmpfs -x squashfs -x overlay | awk '
            BEGIN { printf "[" }
            NR>1 {
                if (NR>2) printf ",";
                printf "{\\"mount\\":\\"%s\\",\\"total_gb\\":%.2f,\\"used_gb\\":%.2f}", $6, $2/1024/1024, $3/1024/1024
            }
            END { printf "]" }'"""
        ]
        run_ssm_batch(online_linux, "AWS-RunShellScript", linux_cmd)

    # 2. Windows: Output all fixed local drives (DriveType=3) as JSON
    if online_windows:
        win_cmd = [
            """$disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
                [PSCustomObject]@{
                    mount = $_.DeviceID
                    total_gb = [math]::Round($_.Size / 1GB, 2)
                    used_gb = [math]::Round(($_.Size - $_.FreeSpace) / 1GB, 2)
                }
            }); ConvertTo-Json -InputObject $disks -Compress"""
        ]
        run_ssm_batch(online_windows, "AWS-RunPowerShellScript", win_cmd)

    return results


def collect_inventory():
    rows = []

    for account_id in TARGET_ACCOUNTS:
        print(f"Scanning Account: {account_id}...")
        session = assume_role(account_id, ROLE_NAME)
        if not session:
            continue

        regions = get_enabled_regions(session)

        for region in regions:
            ec2 = session.client("ec2", region_name=region)
            ssm = session.client("ssm", region_name=region)

            region_instances = []
            paginator = ec2.get_paginator("describe_instances")
            for page in paginator.paginate():
                for reservation in page["Reservations"]:
                    region_instances.extend(reservation["Instances"])

            if not region_instances:
                continue

            running_instances = [
                inst for inst in region_instances if inst["State"]["Name"] == "running"
            ]
            ssm_usage_map = get_ssm_disk_usage_batch(ssm, running_instances)

            for instance in region_instances:
                instance_id = instance["InstanceId"]
                state = instance["State"]["Name"]
                instance_type = instance["InstanceType"]

                name = next(
                    (t["Value"] for t in instance.get("Tags", []) if t["Key"] == "Name"),
                    "Unnamed",
                )

                # Total Provisioned EBS Disk Space (GB)
                volume_ids = [
                    bdm["Ebs"]["VolumeId"]
                    for bdm in instance.get("BlockDeviceMappings", [])
                    if "Ebs" in bdm
                ]

                total_space_gb = 0
                if volume_ids:
                    vols = ec2.describe_volumes(VolumeIds=volume_ids)["Volumes"]
                    total_space_gb = sum(v["Size"] for v in vols)

                # Map SSM Disk Usage & Per-Mount Breakdown
                if state == "running":
                    used_space_gb, mount_details = ssm_usage_map.get(
                        instance_id, ("N/A", "N/A")
                    )
                else:
                    used_space_gb = "Instance Stopped"
                    mount_details = "N/A"

                rows.append({
                    "AccountID": account_id,
                    "Region": region,
                    "InstanceID": instance_id,
                    "State": state,
                    "InstanceType": instance_type,
                    "TotalProvisionedEBS_GB": total_space_gb,
                    "TotalDiskUsed_GB": used_space_gb,
                    "MountDetails": mount_details,
                })

    if rows:
        with open(OUTPUT_CSV, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nSuccess! Exported {len(rows)} instances to {OUTPUT_CSV}")
    else:
        print("\nNo EC2 instances found across specified accounts.")


if __name__ == "__main__":
    collect_inventory()
