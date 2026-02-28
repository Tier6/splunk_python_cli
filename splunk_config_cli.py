import argparse
import json
import requests

def post_splunk_changes(token, host, port, conf_type, data_file, update_only=False):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    try:
        with open(data_file, 'r') as f:
            change_list = json.load(f)
    except Exception as e:
        print(f"Error reading JSON file: {e}")
        return

    for item in change_list:
        title = item.get("title")
        app = item.get("app", "search")
        configs = item.get("configs", {})

        # URL mapping: /servicesNS/nobody/<app>/configs/conf-<type>/<title>
        base_url = f"https://{host}:{port}/servicesNS/nobody/{app}/configs/conf-{conf_type}"
        stanza_url = f"{base_url}/{title}"

        print(f"--- Processing [{title}] in {app} ---")

        # 1. Try to update existing
        response = requests.post(stanza_url, headers=headers, data=configs, verify=False)

        # 2. If 404, the stanza doesn't exist
        if response.status_code == 404:
            if update_only:
                print(f"Stanza '{title}' not found. Skipping (--update-only).")
                continue
            print(f"Stanza '{title}' not found. Attempting to create...")
            create_payload = {**configs, "name": title}
            response = requests.post(base_url, headers=headers, data=create_payload, verify=False)

        if response.status_code in [200, 201]:
            print(f"Successfully applied changes to {title}.")
        else:
            print(f"Failed {title}. Status: {response.status_code}, Body: {response.text}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Splunk Bulk Config CLI")
    parser.add_argument("--token", required=True, help="Splunk JWT")
    parser.add_argument("--host", required=True, help="Splunk Host")
    parser.add_argument("--port", default="8089", help="Mgmt Port (8089)")
    parser.add_argument("--type", required=True, help="e.g. props, savedsearches")
    parser.add_argument("--file", required=True, help="Path to JSON list")
    parser.add_argument("--update-only", action="store_true", help="Only update existing stanzas, skip creation on 404")

    args = parser.parse_args()
    requests.packages.urllib3.disable_warnings()

    post_splunk_changes(args.token, args.host, args.port, args.type, args.file, args.update_only)
