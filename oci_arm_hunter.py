import base64
import os
import random
import tempfile
import time
from pathlib import Path

import oci
import requests
from oci.exceptions import ServiceError


ARM_SHAPE = "VM.Standard.A1.Flex"
ACTIVE_STATES = {
    "CREATING_IMAGE",
    "MOVING",
    "PROVISIONING",
    "RUNNING",
    "STARTING",
    "STOPPED",
    "STOPPING",
}
RETRYABLE_CODES = {
    "InternalError",
    "TooManyRequests",
}
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


def required_env(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def int_env(name, default):
    raw = os.getenv(name, str(default)).strip()
    return int(raw)


def bool_env(name, default=False):
    raw = os.getenv(name, str(default)).strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def csv_env(name):
    raw = os.getenv(name, "").strip()
    return [item.strip() for item in raw.split(",") if item.strip()]


def write_private_key():
    key_b64 = os.getenv("OCI_PRIVATE_KEY_B64", "").strip()
    key_text = os.getenv("OCI_PRIVATE_KEY", "").strip()
    if key_b64:
        key_bytes = base64.b64decode(key_b64)
    elif key_text:
        key_bytes = key_text.encode("utf-8")
    else:
        raise RuntimeError("Missing OCI_PRIVATE_KEY_B64 or OCI_PRIVATE_KEY")

    key_file = tempfile.NamedTemporaryFile("wb", delete=False)
    key_file.write(key_bytes)
    key_file.close()
    Path(key_file.name).chmod(0o600)
    return key_file.name


def build_config():
    return {
        "user": required_env("OCI_USER_OCID"),
        "fingerprint": required_env("OCI_FINGERPRINT"),
        "tenancy": required_env("OCI_TENANCY_OCID"),
        "region": required_env("OCI_REGION"),
        "key_file": write_private_key(),
    }


def notify(message):
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return
    try:
        requests.post(webhook, json={"content": message}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        print(f"Notification failed: {exc}")


def get_availability_domains(identity_client, compartment_id):
    explicit = csv_env("OCI_AVAILABILITY_DOMAINS")
    if explicit:
        return explicit

    suffixes = csv_env("OCI_AD_SUFFIXES") or ["AD-1", "AD-2", "AD-3"]
    response = identity_client.list_availability_domains(compartment_id=compartment_id)
    names = [item.name for item in response.data]
    filtered = [
        name
        for name in names
        if any(name.endswith(suffix) for suffix in suffixes)
    ]
    return filtered or names


def find_subnet(network_client, compartment_id):
    subnet_id = os.getenv("OCI_SUBNET_OCID", "").strip()
    if subnet_id:
        return subnet_id

    subnets = oci.pagination.list_call_get_all_results(
        network_client.list_subnets,
        compartment_id=compartment_id,
    ).data
    candidates = [
        subnet
        for subnet in subnets
        if subnet.lifecycle_state == "AVAILABLE"
        and subnet.prohibit_public_ip_on_vnic is False
    ]
    if not candidates:
        raise RuntimeError("No public subnet found. Set OCI_SUBNET_OCID explicitly.")
    candidates.sort(key=lambda subnet: subnet.time_created or "")
    return candidates[0].id


def find_image(compute_client, compartment_id):
    image_id = os.getenv("OCI_IMAGE_OCID", "").strip()
    if image_id:
        return image_id

    os_name = os.getenv("OCI_IMAGE_OS", "Canonical Ubuntu").strip()
    os_version = os.getenv("OCI_IMAGE_VERSION", "22.04").strip()
    name_contains = os.getenv("OCI_IMAGE_NAME_CONTAINS", "Minimal").strip().lower()
    images = oci.pagination.list_call_get_all_results(
        compute_client.list_images,
        compartment_id=compartment_id,
        shape=ARM_SHAPE,
        operating_system=os_name,
    ).data
    matches = [
        image
        for image in images
        if os_version in (image.operating_system_version or "")
    ]
    if name_contains:
        narrow = [
            image
            for image in matches
            if name_contains in (image.display_name or "").lower()
        ]
        if narrow:
            matches = narrow
    if not matches:
        raise RuntimeError(
            "No matching image found. Set OCI_IMAGE_OCID from the OCI console."
        )
    matches.sort(key=lambda image: image.time_created or "", reverse=True)
    selected = matches[0]
    print(f"Selected image: {selected.display_name}")
    return selected.id


def existing_instance(compute_client, compartment_id, display_name):
    instances = oci.pagination.list_call_get_all_results(
        compute_client.list_instances,
        compartment_id=compartment_id,
        display_name=display_name,
    ).data
    for instance in instances:
        if instance.shape == ARM_SHAPE and instance.lifecycle_state in ACTIVE_STATES:
            return instance
    return None


def should_retry(error):
    msg = error.message or ""
    if "Out of host capacity" in msg or "Out of capacity" in msg:
        return True
    if error.status in RETRYABLE_STATUSES:
        return True
    if error.code in RETRYABLE_CODES:
        return True
    return False


def launch_once(compute_client, compartment_id, subnet_id, image_id, ad_name):
    display_name = required_env("OCI_INSTANCE_NAME")
    ssh_public_key = required_env("SSH_PUBLIC_KEY")
    boot_gb = max(50, int_env("BOOT_VOLUME_SIZE_GB", 200))
    assign_public_ip = bool_env("ASSIGN_PUBLIC_IP", True)

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad_name,
        compartment_id=compartment_id,
        display_name=display_name,
        shape=ARM_SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=4,
            memory_in_gbs=24,
        ),
        availability_config=oci.core.models.LaunchInstanceAvailabilityConfigDetails(
            recovery_action="RESTORE_INSTANCE"
        ),
        instance_options=oci.core.models.InstanceOptions(
            are_legacy_imds_endpoints_disabled=True
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            assign_private_dns_record=True,
            assign_public_ip=assign_public_ip,
            display_name=display_name,
            subnet_id=subnet_id,
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            source_type="image",
            image_id=image_id,
            boot_volume_size_in_gbs=boot_gb,
        ),
        metadata={"ssh_authorized_keys": ssh_public_key},
    )
    return compute_client.launch_instance(launch_instance_details=details).data


def main():
    display_name = required_env("OCI_INSTANCE_NAME")
    interval = int_env("ATTEMPT_INTERVAL_SECONDS", 45)
    max_runtime = int_env("MAX_RUNTIME_SECONDS", 21000)
    started_at = time.monotonic()

    config = build_config()
    oci.config.validate_config(config)
    tenancy_id = required_env("OCI_TENANCY_OCID")
    compartment_id = os.getenv("OCI_COMPARTMENT_OCID", "").strip() or tenancy_id

    identity_client = oci.identity.IdentityClient(config)
    network_client = oci.core.VirtualNetworkClient(config)
    compute_client = oci.core.ComputeClient(config)

    ad_names = get_availability_domains(identity_client, tenancy_id)
    if not ad_names:
        raise RuntimeError("No availability domains found.")
    subnet_id = find_subnet(network_client, compartment_id)
    image_id = find_image(compute_client, compartment_id)

    print(
        f"Starting OCI A1 hunter for {display_name}; "
        f"ADs={', '.join(ad_names)}; interval={interval}s; boot=200GB"
    )
    notify(f"OCI A1 hunter started for {display_name}.")

    attempt = 0
    while time.monotonic() - started_at < max_runtime:
        existing = existing_instance(compute_client, compartment_id, display_name)
        if existing:
            msg = f"Instance already exists: {existing.display_name} ({existing.lifecycle_state})."
            print(msg)
            notify(msg)
            return

        attempt += 1
        ad_name = ad_names[(attempt - 1) % len(ad_names)]
        print(f"Attempt {attempt}: trying {ad_name}")
        try:
            instance = launch_once(
                compute_client=compute_client,
                compartment_id=compartment_id,
                subnet_id=subnet_id,
                image_id=image_id,
                ad_name=ad_name,
            )
            msg = (
                f"Created instance {instance.display_name}; "
                f"state={instance.lifecycle_state}; AD={instance.availability_domain}"
            )
            print(msg)
            notify(msg)
            return
        except ServiceError as exc:
            clean_message = (exc.message or "").splitlines()[0][:240]
            print(
                f"Attempt {attempt} failed: status={exc.status}, "
                f"code={exc.code}, message={clean_message}"
            )
            if not should_retry(exc):
                notify(
                    f"OCI A1 hunter stopped on non-retryable error: "
                    f"{exc.status} {exc.code}"
                )
                raise

        sleep_for = interval + random.randint(0, min(15, interval))
        time.sleep(sleep_for)

    print("Max runtime reached; let the next scheduled workflow continue.")
    notify("OCI A1 hunter reached max runtime; next schedule will continue.")


if __name__ == "__main__":
    main()
