import argparse
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter


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


def create_session(token, pool_size=10):
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    })
    session.verify = False

    adapter = HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
    )
    session.mount("https://", adapter)
    return session


def post_splunk_changes(session, host, port, conf_type, data_file,
                        update_only=False, test_run=None, workers=8,
                        log_file=None):
    log = setup_logging(log_file)

    try:
        with open(data_file, 'r') as f:
            change_list = json.load(f)
    except Exception as e:
        log.error(f"Error reading JSON file: {e}")
        return None

    log.info(f"Loaded {len(change_list)} stanza(s) from {data_file}")

    if test_run is not None:
        log.info(f"--test-run {test_run}: processing first {test_run} of {len(change_list)} stanza(s)")
        change_list = change_list[:test_run]

    log.info(f"Target: https://{host}:{port} | conf-type: {conf_type or 'N/A (post-by-id)'} | update-only: {update_only} | workers: {workers}")

    def _push_one(item):
        title = item.get("title")
        app = item.get("app", "search")
        configs = item.get("configs", {})
        item_id = item.get("id")

        if item_id:
            parsed_id = urlparse(item_id)
            stanza_url = f"https://{host}:{port}{parsed_id.path}"
            base_url = f"https://{host}:{port}{parsed_id.path.rsplit('/', 1)[0]}"
        elif conf_type:
            base_url = f"https://{host}:{port}/servicesNS/nobody/{app}/configs/conf-{conf_type}"
            stanza_url = f"{base_url}/{title}"
        else:
            log.error(f"Stanza '{title}' has no id and --type was not provided. Skipping.")
            return "failed"

        log.info(f"Processing [{title}] in app={app}")

        response = session.post(stanza_url, data=configs)

        if response.status_code == 404:
            if update_only:
                log.warning(f"Stanza '{title}' not found. Skipping (--update-only).")
                return "skipped"
            log.info(f"Stanza '{title}' not found. Attempting to create...")
            create_payload = {**configs, "name": title}
            response = session.post(base_url, data=create_payload)

        if response.status_code in [200, 201]:
            log.info(f"Successfully applied changes to {title}.")
            return "success"
        else:
            log.error(f"Failed {title}. Status: {response.status_code}, Body: {response.text}")
            return "failed"

    success, failed, skipped = 0, 0, 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_push_one, item): item for item in change_list}
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
            except Exception as e:
                log.error(f"Unexpected error processing '{item.get('title', '?')}': {e}")
                failed += 1
                continue
            if result == "success":
                success += 1
            elif result == "failed":
                failed += 1
            elif result == "skipped":
                skipped += 1

    log.info(f"Complete: {success} succeeded, {failed} failed, {skipped} skipped")

    return change_list


def validate_shc(session, host, port, conf_type, change_list,
                 delay=5, workers=8, log_file=None):
    log = setup_logging(log_file)

    # Step A — Get captain info & verify health
    log.info("SHC Validation: checking captain health...")
    captain_url = f"https://{host}:{port}/services/shcluster/captain/info?output_mode=json"
    try:
        resp = session.get(captain_url)
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
        resp = session.get(members_url)
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

    # Step C — Validate configs on each member (threaded)
    def _validate_one(item, member):
        title = item.get("title")
        app = item.get("app", "search")
        configs = item.get("configs", {})
        item_id = item.get("id")

        if item_id:
            parsed_id = urlparse(item_id)
            stanza_url = (f"https://{member['host']}:{member['port']}"
                          f"{parsed_id.path}?output_mode=json")
        else:
            stanza_url = (f"https://{member['host']}:{member['port']}"
                          f"/servicesNS/nobody/{app}/configs/conf-{conf_type}"
                          f"/{title}?output_mode=json")

        try:
            resp = session.get(stanza_url)
        except requests.RequestException as e:
            log.error(f"  [{member['label']}] {title}: ERROR ({e})")
            return "missing"

        if resp.status_code == 404:
            log.warning(f"  [{member['label']}] {title}: MISSING")
            return "missing"

        if resp.status_code != 200:
            log.error(f"  [{member['label']}] {title}: ERROR (HTTP {resp.status_code})")
            return "missing"

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
            return "drift"
        else:
            log.info(f"  [{member['label']}] {title}: PASS")
            return "passed"

    check_pairs = [(item, member) for item in change_list for member in members]
    total_checks = len(check_pairs)
    passed = drift = missing = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_validate_one, item, member): (item, member)
            for item, member in check_pairs
        }
        for future in as_completed(futures):
            item, member = futures[future]
            try:
                result = future.result()
            except Exception as e:
                log.error(f"  [{member['label']}] {item.get('title', '?')}: "
                          f"UNEXPECTED ERROR ({e})")
                missing += 1
                continue
            if result == "passed":
                passed += 1
            elif result == "drift":
                drift += 1
            elif result == "missing":
                missing += 1

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
    parser.add_argument("--type", help="e.g. props, savedsearches (required unless --post-by-id)")
    parser.add_argument("--file", required=True, help="Path to JSON list")
    parser.add_argument("--post-by-id", action="store_true",
        help="Use the id field from each JSON item as the request URL; --type becomes optional")
    parser.add_argument("--update-only", action="store_true",
        help="Only update existing stanzas, skip creation on 404")
    parser.add_argument("--log", metavar="PATH",
        help="Write log output to a file at the specified path")
    parser.add_argument("--shc", action="store_true",
        help="Validate configs replicated to all SHC members after push")
    parser.add_argument("--shc-delay", type=int, default=5, metavar="SECONDS",
        help="Seconds to wait for SHC replication before validating (default: 5)")
    parser.add_argument("--test-run", type=int, default=None, metavar="N",
        help="Only process the first N items (validates connectivity before full run)")
    parser.add_argument("--workers", type=int, default=8, metavar="N",
        help="Number of concurrent threads for push/validation (default: 8)")

    args = parser.parse_args()

    if not args.post_by_id and not args.type:
        parser.error("--type is required unless --post-by-id is specified")

    requests.packages.urllib3.disable_warnings()

    session = create_session(args.token, pool_size=args.workers)

    change_list = post_splunk_changes(
        session, args.host, args.port, args.type, args.file,
        args.update_only, args.test_run, args.workers, args.log
    )

    if args.shc and change_list:
        validate_shc(session, args.host, args.port, args.type,
                     change_list, args.shc_delay, args.workers, args.log)
