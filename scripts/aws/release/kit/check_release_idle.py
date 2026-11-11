"""便の開始前に、進行中のビルド・差し替えを読み取りだけで確認する。"""

import json
import subprocess
import sys

PROJECTS = (
    "teamagent-dev-approval-publisher",
    "teamagent-dev-mcp-source-publisher",
    "teamagent-dev-image-builder",
    "teamagent-dev-image-attestor",
    "teamagent-dev-image-promoter",
)
SERVICES = (
    "teamagent-dev-mcp",
    "teamagent-dev-openclaw",
    "teamagent-dev-connect-web",
)


def aws(*args):
    return json.loads(subprocess.check_output(["aws", *args, "--output", "json"], text=True))


def main():
    busy = False
    for project in PROJECTS:
        # AWS CLI の自動ページングで古い進行中ビルドも取りこぼさない。
        ids = aws("codebuild", "list-builds-for-project", "--project-name", project)["ids"]
        for start in range(0, len(ids), 100):
            batch = ids[start : start + 100]
            result = aws("codebuild", "batch-get-builds", "--ids", *batch)
            if result.get("buildsNotFound") or len(result["builds"]) != len(batch):
                raise ValueError(f"{project}: ビルドの状態を確認できない")
            for build in result["builds"]:
                if build["buildStatus"] == "IN_PROGRESS":
                    print(f"STOP: ビルド進行中 {build['id']} initiator={build['initiator']}")
                    busy = True
    result = aws("ecs", "describe-services", "--cluster", "teamagent-dev", "--services", *SERVICES)
    if result.get("failures") or {s["serviceName"] for s in result["services"]} != set(SERVICES):
        raise ValueError("ECS 3サービスの状態を確認できない")
    for service in result["services"]:
        deployments = service["deployments"]
        if not deployments or not any(d["status"] == "PRIMARY" for d in deployments):
            raise ValueError(f"{service['serviceName']}: PRIMARY を確認できない")
        for deployment in deployments:
            if deployment["status"] != "PRIMARY":
                print(f"STOP: 差し替え中 {service['serviceName']} deployment={deployment['id']}")
                busy = True
    if busy:
        print(
            "停止: 進行中ビルドがない差し替えの実行者は ECS からは判別できません。担当者に確認してください。"
        )
        return 1
    print("OK: 進行中の mcp 便・サービス差し替えなし")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (subprocess.CalledProcessError, ValueError, KeyError, TypeError, OSError) as error:
        print(f"STOP: 同時実行の確認に失敗: {error}", file=sys.stderr)
        sys.exit(1)
