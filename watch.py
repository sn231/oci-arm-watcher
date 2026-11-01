#!/usr/bin/env python3
"""Poll OCI host capacity for Always Free compute and launch the first available target.

Targets, in priority order: A1 2C/12G, A1 1C/6G, A1 1C/1G, AMD micro 1C/1G.
Read-only until capacity actually appears. Refuses to launch beyond the Always Free
entitlement (2 OCPU / 12 GB A1 total, 2 AMD micro instances), so this cannot bill.
"""

import argparse
import atexit
import datetime
import json
import os
import random
import subprocess
import sys
import time

import oci
from oci.core import ComputeClient, VirtualNetworkClient
from oci.core.models import (
    CapacityReportInstanceShapeConfig,
    CreateCapacityReportShapeAvailabilityDetails,
    CreateComputeCapacityReportDetails,
    CreateVnicDetails,
    InstanceSourceViaImageDetails,
    LaunchInstanceDetails,
    LaunchInstanceShapeConfigDetails,
)

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE, "watch.log")
SUCCESS_PATH = os.path.join(BASE, "SUCCESS.json")
LOCK_PATH = os.path.join(BASE, "watch.pid")

AD = os.environ.get("OCI_AD", "cmbf:AP-SINGAPORE-1-AD-1")
A1_SHAPE = "VM.Standard.A1.Flex"
AMD_SHAPE = "VM.Standard.E2.1.Micro"
A1_IMAGE_ID = os.environ.get(
    "OCI_IMAGE_A1",
    "ocid1.image.oc1.ap-singapore-1.aaaaaaaaw75ef2chh5goomskvdznqz36cadczqelqvndbqcozc5icyig57za",
)
AMD_IMAGE_ID = os.environ.get(
    "OCI_IMAGE_AMD",
    "ocid1.image.oc1.ap-singapore-1.aaaaaaaaphw26zauzzsolz7s2zq7e2zusbk4uu2ezzsjurrf5yfdsaq7rysq",
)
SUBNET_ID = os.environ.get(
    "OCI_SUBNET_ID",
    "ocid1.subnet.oc1.ap-singapore-1.aaaaaaaazq5bgwotd2z5etcksnspqq64st5smpjamr5sgtjnthycyj6xpaia",
)
SSH_PUB_PATH = os.path.join(os.path.expanduser("~"), ".ssh", "oci_a1_singapore.pub")

BOOT_GBS = 50
A1_NAME = "weport-a1-sg"
AMD_NAME = "weport-amd-sg"

FREE_A1_OCPUS = 2.0
FREE_A1_MEMORY_GBS = 12.0
FREE_AMD_INSTANCES = 2
ACTIVE_STATES = {"PROVISIONING", "RUNNING", "STARTING", "STOPPING", "STOPPED"}

TARGETS = [
    {"label": "A1 2C/12G", "shape": A1_SHAPE, "ocpus": 2.0, "memory_gbs": 12.0, "image_id": A1_IMAGE_ID, "name": A1_NAME},
    {"label": "A1 1C/6G", "shape": A1_SHAPE, "ocpus": 1.0, "memory_gbs": 6.0, "image_id": A1_IMAGE_ID, "name": A1_NAME},
    {"label": "A1 1C/1G", "shape": A1_SHAPE, "ocpus": 1.0, "memory_gbs": 1.0, "image_id": A1_IMAGE_ID, "name": A1_NAME},
    {"label": "AMD 1C/1G", "shape": AMD_SHAPE, "ocpus": None, "memory_gbs": None, "image_id": AMD_IMAGE_ID, "name": AMD_NAME},
]


def log(message: str) -> None:
    line = f"[{datetime.datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def notify(message: str) -> None:
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;"
        "$n.Visible=$true;"
        f"$n.ShowBalloonTip(10000,'Weport OCI','{message}',0);"
        "Start-Sleep -Seconds 12;$n.Dispose()"
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def pid_alive(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return str(pid) in (result.stdout or "")
    except Exception:
        return False


def acquire_lock() -> bool:
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as handle:
                pid = int(handle.read().strip())
        except Exception:
            pid = 0
        if pid and pid_alive(pid):
            log(f"another watcher is already running (pid {pid})")
            return False
        os.remove(LOCK_PATH)
    descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    atexit.register(lambda: os.path.exists(LOCK_PATH) and os.remove(LOCK_PATH))
    return True


def active_instances(compute: ComputeClient, compartment_id: str):
    return [
        item
        for item in compute.list_instances(compartment_id).data
        if item.lifecycle_state in ACTIVE_STATES
    ]


def allowed_targets(existing):
    a1 = [item for item in existing if item.shape == A1_SHAPE and item.shape_config is not None]
    used_ocpus = sum(float(item.shape_config.ocpus or 0) for item in a1)
    used_memory = sum(float(item.shape_config.memory_in_gbs or 0) for item in a1)
    amd_count = sum(1 for item in existing if item.shape == AMD_SHAPE)
    targets = []
    for target in TARGETS:
        if target["shape"] == A1_SHAPE:
            if target["ocpus"] <= FREE_A1_OCPUS - used_ocpus and target["memory_gbs"] <= FREE_A1_MEMORY_GBS - used_memory:
                targets.append(target)
        elif amd_count < FREE_AMD_INSTANCES:
            targets.append(target)
    return targets, used_ocpus, used_memory, amd_count


def status_key(shape: str, ocpus, memory_gbs):
    def norm(value):
        return round(float(value), 2) if value is not None else None

    return shape, norm(ocpus), norm(memory_gbs)


def capacity_status(compute: ComputeClient, compartment_id: str, targets):
    details = CreateComputeCapacityReportDetails(
        compartment_id=compartment_id,
        availability_domain=AD,
        shape_availabilities=[
            CreateCapacityReportShapeAvailabilityDetails(
                instance_shape=target["shape"],
                instance_shape_config=(
                    CapacityReportInstanceShapeConfig(
                        ocpus=target["ocpus"],
                        memory_in_gbs=target["memory_gbs"],
                    )
                    if target["ocpus"] is not None
                    else None
                ),
            )
            for target in targets
        ],
    )
    report = compute.create_compute_capacity_report(details).data
    statuses = {}
    for item in report.shape_availabilities:
        config = item.instance_shape_config
        key = status_key(
            item.instance_shape,
            config.ocpus if config is not None else None,
            config.memory_in_gbs if config is not None else None,
        )
        statuses[key] = str(item.availability_status)
    return statuses


def launch(compute: ComputeClient, compartment_id: str, ssh_public_key: str, target):
    kwargs = {
        "compartment_id": compartment_id,
        "availability_domain": AD,
        "display_name": target["name"],
        "shape": target["shape"],
        "source_details": InstanceSourceViaImageDetails(
            image_id=target["image_id"],
            boot_volume_size_in_gbs=BOOT_GBS,
        ),
        "create_vnic_details": CreateVnicDetails(
            subnet_id=SUBNET_ID,
            assign_public_ip=True,
            display_name=f"{target['name']}-vnic",
        ),
        "metadata": {"ssh_authorized_keys": ssh_public_key},
    }
    if target["ocpus"] is not None:
        kwargs["shape_config"] = LaunchInstanceShapeConfigDetails(
            ocpus=target["ocpus"],
            memory_in_gbs=target["memory_gbs"],
        )
    return compute.launch_instance(LaunchInstanceDetails(**kwargs)).data


def wait_for_address(compute: ComputeClient, network: VirtualNetworkClient, compartment_id: str, instance_id: str):
    for _ in range(60):
        attachments = compute.list_vnic_attachments(compartment_id, instance_id=instance_id).data
        if attachments:
            vnic = network.get_vnic(attachments[0].vnic_id).data
            if vnic.public_ip:
                return vnic.public_ip
        time.sleep(5)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="check capacity once and exit")
    parser.add_argument("--interval", type=int, default=150, help="poll interval in seconds")
    args = parser.parse_args()

    if not args.once and not acquire_lock():
        return 0

    log("watcher process starting")

    if os.path.exists(SUCCESS_PATH):
        log(f"already created, see {SUCCESS_PATH}; refusing to create a second instance")
        return 0
    ssh_public_key = os.environ.get("OCI_SSH_PUBLIC_KEY", "").strip()
    if not ssh_public_key:
        if not os.path.exists(SSH_PUB_PATH):
            log(f"missing SSH public key: {SSH_PUB_PATH}")
            return 2
        with open(SSH_PUB_PATH, "r", encoding="utf-8") as handle:
            ssh_public_key = handle.read().strip()

    try:
        config = oci.config.from_file()
    except Exception as error:
        log(f"cannot load OCI config: {error}")
        return 2
    compartment_id = config["tenancy"]
    compute = ComputeClient(config)
    network = VirtualNetworkClient(config)

    # A logon task can fire before networking is ready; retry instead of dying silently.
    while True:
        try:
            existing = active_instances(compute, compartment_id)
            break
        except oci.exceptions.ServiceError as error:
            if error.status in (401, 403, 404):
                log(f"fatal API error {error.status}: {error.message}")
                return 4
            log(f"initial instance list failed ({error.status}): {error.message}; retrying in 60s")
        except Exception as error:
            log(f"initial instance list error: {error}; retrying in 60s")
        time.sleep(60)

    for item in existing:
        log(f"existing instance: {item.display_name} shape={item.shape} state={item.lifecycle_state} id={item.id}")

    if any(item.display_name in (A1_NAME, AMD_NAME) for item in existing):
        log("watcher instance already exists; nothing to do")
        return 0

    targets, used_ocpus, used_memory, amd_count = allowed_targets(existing)
    if not targets:
        log(
            f"free entitlement used up (A1 {used_ocpus:g}C/{used_memory:g}G, "
            f"AMD {amd_count}/{FREE_AMD_INSTANCES}); refusing to launch"
        )
        return 3

    log(
        f"start: region={config['region']} ad={AD} "
        f"targets={[target['label'] for target in targets]} boot={BOOT_GBS}G"
    )

    while True:
        try:
            statuses = capacity_status(compute, compartment_id, targets)
        except oci.exceptions.ServiceError as error:
            if error.status == 429:
                log("rate limited (429), backing off 300s")
                time.sleep(300)
                continue
            if error.status in (401, 403, 404):
                log(f"fatal API error {error.status}: {error.message}")
                return 4
            log(f"capacity report failed ({error.status}): {error.message}; retrying")
            time.sleep(args.interval)
            continue
        except Exception as error:  # network hiccups should not kill the watcher
            log(f"capacity report error: {error}; retrying")
            time.sleep(args.interval)
            continue

        chosen = next(
            (
                target
                for target in targets
                if statuses.get(status_key(target["shape"], target["ocpus"], target["memory_gbs"])) == "AVAILABLE"
            ),
            None,
        )
        if chosen is None:
            summary = ", ".join(
                f"{target['label']}={statuses.get(status_key(target['shape'], target['ocpus'], target['memory_gbs']), 'UNKNOWN')}"
                for target in targets
            )
            if all(
                statuses.get(status_key(target["shape"], target["ocpus"], target["memory_gbs"]))
                == "HARDWARE_NOT_SUPPORTED"
                for target in targets
            ):
                log(f"no target shape supported in this availability domain ({summary}); stopping")
                return 5
            log(f"capacity: {summary}")
            if args.once:
                return 0
            time.sleep(args.interval + random.randint(0, 30))
            continue

        log(f"capacity AVAILABLE for {chosen['label']}, launching instance")
        try:
            instance = launch(compute, compartment_id, ssh_public_key, chosen)
        except oci.exceptions.ServiceError as error:
            message = f"{error.message} {error.code or ''}".lower()
            if "capacity" in message or error.status >= 500:
                log(f"launch raced and lost capacity ({error.status}): {error.message}; resuming polling")
                if args.once:
                    return 0
                time.sleep(args.interval + random.randint(0, 30))
                continue
            log(f"launch failed {error.status}: {error.message}")
            return 6

        public_ip = wait_for_address(compute, network, compartment_id, instance.id)
        result = {
            "id": instance.id,
            "display_name": instance.display_name,
            "shape": instance.shape,
            "target": chosen["label"],
            "ocpus": chosen["ocpus"],
            "memory_in_gbs": chosen["memory_gbs"],
            "state": instance.lifecycle_state,
            "public_ip": public_ip,
            "ssh_user": "ubuntu",
            "ssh_key": os.path.join(os.path.expanduser("~"), ".ssh", "oci_a1_singapore"),
            "created_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        with open(SUCCESS_PATH, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        log(f"created {instance.id} {chosen['label']}" + ("" if os.environ.get("GITHUB_ACTIONS") else f" ip={public_ip or 'pending'}"))
        notify(f"{chosen['label']} ready: {public_ip or instance.id}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
