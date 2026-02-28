import argparse
import json
import logging
import time
from urllib.parse import urlparse

import requests

def setup_logging(log_file=None):
    logger = logging.getLogger("splunk_config_cli")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger

def post_splunk_changes(token, host, port, conf_type, data_file, update_only=False, log_file=None):
    log = setup_logging(log_file)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        with open(data_file, 'r') as f:
            change_list = json.load(f)
    except Exception as e:
        log.error(f"Error reading JSON file: {e}")
        return None

    log.info(f"Loaded {len(change_list)} stanza(s) from {data_file}")
    log.info(f"Target: https://{host}:{port} | conf-type: {conf_type} | update-only: {update_only}")

    success, failed, skipped = 0, 0, 0

    for item in change_list:
        title = item.get("title")
        app = item.get("app", "search")
        configs = item.get("configs", {})
        item_id = item.get("id")

        if item_id:
            # Favor id: use its path but target the CLI-specified host:port
            parsed_id = urlparse(item_id)
            stanza_url = f"https://{host}:{port}{parsed_id.path}"
            base_url = f"https://{host}:{port}{parsed_id.path.rsplit('/', 1)[0]}"
        else:
            # Fallback: construct from app/type/title
            base_url = f"https://{host}:{port}/servicesNS/nobody/{app}/configs/conf-{conf_type}"
            stanza_url = f"{base_url}/{title}"

        log.info(f"Processing [{title}] in app={app}")

        # 1. Try to update existing
        response = requests.post(stanza_url, headers=headers, data=configs, verify=False)

        # 2. If 404, the stanza doesn't exist
        if response.status_code == 404:
            if update_only:
                log.warning(f"Stanza '{title}' not found. Skipping (--update-only).")
                skipped += 1
                continue
            log.info(f"Stanza '{title}' not found. Attempting to create...")
            create_payload = {**configs, "name": title}
            response = requests.post(base_url, headers=headers, data=create_payload, verify=False)

        if response.status_code in [200, 201]:
            log.info(f"Successfully applied changes to {title}.")
            success += 1
        else:
            log.error(f"Failed {title}. Status: {response.status_code}, Body: {response.text}")
            failed += 1

    log.info(f"Complete: {success} succeeded, {failed} failed, {skipped} skipped")

    return change_list


def validate_shc(token, host, port, conf_type, change_list, delay=5, log_file=None):
    log = setup_logging(log_file)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    # Step A — Get captain info & verify health
    log.info("SHC Validation: checking captain health...")
    captain_url = f"https://{host}:{port}/services/shcluster/captain/info?output_mode=json"
    try:
        resp = requests.get(captain_url, headers=headers, verify=False)
        if resp.status_code in (404, 503):
            log.warning("SHC Validation: this host does not appear to be an SHC member. Skipping validation.")
            log.info(f"Captain info returned HTTP {resp.status_code}: {resp.text}")
            return
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"SHC Validation: failed to reach captain info endpoint: {e}")
        return

    captain_info = resp.json()["entry"][0]["content"]
    if str(captain_info.get("service_ready_flag")) != "1":
        log.error("SHC Validation: cluster is not ready (service_ready_flag != 1). Aborting.")
        return

    captain_label = captain_info.get("label", "unknown")
    log.info(f"SHC Validation: captain '{captain_label}' is healthy (service_ready_flag=1)")

    # Step B — Discover members
    members_url = f"https://{host}:{port}/services/shcluster/captain/members?output_mode=json"
    try:
        resp = requests.get(members_url, headers=headers, verify=False)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"SHC Validation: failed to discover members: {e}")
        return

    members = []
    for entry in resp.json().get("entry", []):
        content = entry.get("content", {})
        mgmt_uri = content.get("management_uri", "")
        label = content.get("label", entry.get("name", "unknown"))
        if mgmt_uri:
            parsed = urlparse(mgmt_uri)
            members.append({
                "label": label,
                "host": parsed.hostname,
                "port": parsed.port or 8089,
            })

    if not members:
        log.error("SHC Validation: no members discovered. Aborting.")
        return

    log.info(f"SHC Validation: discovered {len(members)} member(s): "
             + ", ".join(m["label"] for m in members))

    # Wait for replication
    log.info(f"SHC Validation: waiting {delay}s for knowledge bundle replication...")
    time.sleep(delay)

    # Step C — Validate configs on each member
    total_checks = 0
    passed = 0
    drift = 0
    missing = 0

    for item in change_list:
        title = item.get("title")
        app = item.get("app", "search")
        configs = item.get("configs", {})
        item_id = item.get("id")

        for member in members:
            total_checks += 1
            if item_id:
                parsed_id = urlparse(item_id)
                stanza_url = (f"https://{member['host']}:{member['port']}"
                              f"{parsed_id.path}?output_mode=json")
            else:
                stanza_url = (f"https://{member['host']}:{member['port']}"
                              f"/servicesNS/nobody/{app}/configs/conf-{conf_type}"
                              f"/{title}?output_mode=json")

            try:
                resp = requests.get(stanza_url, headers=headers, verify=False)
            except requests.RequestException as e:
                log.error(f"  [{member['label']}] {title}: ERROR ({e})")
                missing += 1
                continue

            if resp.status_code == 404:
                log.warning(f"  [{member['label']}] {title}: MISSING")
                missing += 1
                continue

            if resp.status_code != 200:
                log.error(f"  [{member['label']}] {title}: ERROR (HTTP {resp.status_code})")
                missing += 1
                continue

            remote = resp.json()["entry"][0]["content"]
            mismatched = []
            for key, expected in configs.items():
                actual = str(remote.get(key, ""))
                if actual != str(expected):
                    mismatched.append(f"{key}: expected={expected!r}, got={actual!r}")

            if mismatched:
                log.warning(f"  [{member['label']}] {title}: DRIFT")
                for m in mismatched:
                    log.warning(f"    {m}")
                drift += 1
            else:
                log.info(f"  [{member['label']}] {title}: PASS")
                passed += 1

    # Step D — Summary
    stanza_count = len(change_list)
    member_count = len(members)
    log.info(f"SHC Validation: {stanza_count}/{stanza_count} stanzas verified across "
             f"{member_count} members ({passed}/{total_checks} checks passed"
             + (f", {drift} drift, {missing} missing" if drift or missing else "")
             + ")")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Splunk Bulk Config CLI")
    parser.add_argument("--token", required=True, help="Splunk JWT")
    parser.add_argument("--host", required=True, help="Splunk Host")
    parser.add_argument("--port", default="8089", help="Mgmt Port (8089)")
    parser.add_argument("--type", required=True, help="e.g. props, savedsearches")
    parser.add_argument("--file", required=True, help="Path to JSON list")
    parser.add_argument("--update-only", action="store_true", help="Only update existing stanzas, skip creation on 404")
    parser.add_argument("--log", metavar="PATH", help="Write log output to a file at the specified path")
    parser.add_argument("--shc", action="store_true",
        help="Validate configs replicated to all SHC members after push")
    parser.add_argument("--shc-delay", type=int, default=5, metavar="SECONDS",
        help="Seconds to wait for SHC replication before validating (default: 5)")

    args = parser.parse_args()
    requests.packages.urllib3.disable_warnings()

    change_list = post_splunk_changes(args.token, args.host, args.port, args.type, args.file, args.update_only, args.log)

    if args.shc and change_list:
        validate_shc(args.token, args.host, args.port, args.type, change_list, args.shc_delay, args.log)
