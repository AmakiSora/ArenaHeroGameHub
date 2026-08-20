"""One-off smoke test for the /api/unit-routes endpoint on the VPS."""
import os

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
env = {}
with open(os.path.join(HERE, ".env.deploy"), encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = value

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(env["DEPLOY_HOST"], port=int(env.get("DEPLOY_PORT", 22)),
               username=env.get("DEPLOY_USER", "root"),
               password=env["DEPLOY_PASSWORD"])
command = (f'curl -s -H "Authorization: Bearer {env["DASHBOARD_TOKEN"]}" '
           'http://127.0.0.1:4399/api/unit-routes | head -c 700')
_, stdout, _ = client.exec_command(command)
print(stdout.read().decode())
client.close()
