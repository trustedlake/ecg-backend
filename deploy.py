#!/usr/bin/env python3
"""ECG production helper. Python 3.9+, standard library, Windows/macOS/Linux."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
ACCOUNT = "785236377332"
REGION = "us-east-1"
URL = "https://7y0j5a8yte.execute-api.us-east-1.amazonaws.com"
PIPELINES = ("ecg-lambdas", "ecg-backend")


def run(argv, env=None):
    result = subprocess.run(argv, env=env, text=True, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def aws(args, *argv):
    command = ["aws", "--region", REGION, "--output", "json", "--no-cli-pager"]
    if args.profile:
        command += ["--profile", args.profile]
    output = run(command + list(argv))
    return json.loads(output) if output.strip() else {}


def identity(args):
    caller = aws(args, "sts", "get-caller-identity")
    print(f"Account: {caller['Account']} | Region: {REGION} | Identity: {caller['Arn']}")
    if caller["Account"] != ACCOUNT:
        raise RuntimeError(f"Expected account {ACCOUNT}; refusing to use another account.")


def health():
    with urllib.request.urlopen(URL + "/api/health/", timeout=30) as response:
        data = json.load(response)
    print("Health:", json.dumps(data))
    if data.get("status") != "ok" or data.get("db") != "ok":
        raise RuntimeError("API/database health check failed.")
    with urllib.request.urlopen(URL + "/", timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("Web application check failed.")


def status(args):
    failed = False
    for name in PIPELINES:
        executions = aws(args, "codepipeline", "list-pipeline-executions",
                         "--pipeline-name", name, "--max-results", "1")["pipelineExecutionSummaries"]
        if not executions:
            print(f"{name}: never executed")
            failed = True
        else:
            entry = executions[0]
            print(f"{name}: {entry['status']} ({entry['pipelineExecutionId']})")
            failed |= entry["status"] != "Succeeded"
    health()
    if failed:
        raise RuntimeError("At least one pipeline has no successful latest execution.")


def doctor(args):
    """Check local prerequisites before requiring AWS authentication."""
    for tool in ("aws", "git", "terraform"):
        location = shutil.which(tool)
        print(f"{tool}: {location or 'MISSING'}")
        if not location:
            raise RuntimeError(f"Install {tool} and add it to PATH.")
    version = json.loads(run(["terraform", "version", "-json"]))["terraform_version"]
    if tuple(map(int, version.split(".")[:2])) < (1, 11):
        raise RuntimeError("Terraform >= 1.11 is required.")
    identity(args)
    for name in PIPELINES:
        aws(args, "codepipeline", "get-pipeline", "--name", name)
        print(f"Pipeline {name}: accessible")
    aws(args, "s3api", "head-bucket", "--bucket",
        "ecg-terraform-state-785236377332-us-east-1")
    print("Production state bucket: accessible")
    health()
    print("Existing environment verified. New-account bootstrap: docs/bootstrap-reference.md")


def plan(args):
    if not shutil.which("terraform"):
        raise RuntimeError("Install Terraform >= 1.11.")
    env = os.environ.copy()
    if args.profile:
        env["AWS_PROFILE"] = args.profile
    env["AWS_DEFAULT_REGION"] = REGION
    prefix = ["terraform", f"-chdir={ROOT / 'infra' / 'aws'}"]
    # No saved plan: inspection only; this helper never applies infrastructure.
    for command in (["init", "-input=false"],
                    ["plan", "-input=false", "-lock-timeout=60s",
                     f"-var=aws_profile={args.profile or ''}",
                     f"-var=region={REGION}"]):
        result = subprocess.run(prefix + command, env=env)
        if result.returncode:
            raise RuntimeError(f"Terraform {command[0]} failed ({result.returncode}).")


def deploy(args):
    names = PIPELINES if args.pipeline == "all" else (args.pipeline,)
    for name in names:
        # Avoid starting another release while an existing one is active/queued.
        recent = aws(args, "codepipeline", "list-pipeline-executions",
                     "--pipeline-name", name, "--max-results", "100")
        if any(x["status"] == "InProgress" for x in recent["pipelineExecutionSummaries"]):
            raise RuntimeError(f"{name} already has an active execution; use status or the AWS console.")
        execution = aws(args, "codepipeline", "start-pipeline-execution", "--name", name)["pipelineExecutionId"]
        print(f"Started {name}: {execution}", flush=True)
        deadline = time.monotonic() + args.timeout
        previous = None
        while time.monotonic() < deadline:
            state = aws(args, "codepipeline", "get-pipeline-execution", "--pipeline-name", name,
                        "--pipeline-execution-id", execution)["pipelineExecution"]["status"]
            if state != previous:
                print(f"{name}: {state}", flush=True)
                previous = state
            if state == "Succeeded":
                break
            if state not in ("InProgress", "Stopping"):
                raise RuntimeError(f"{name} ended with {state}. Inspect the pipeline logs.")
            time.sleep(15)
        else:
            raise RuntimeError(f"Wait timed out for {name}/{execution}; AWS continues running. Use status.")
    health()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("doctor", "plan", "deploy", "status"))
    parser.add_argument("--profile", default=None, help="AWS CLI profile; default: AWS credential chain")
    parser.add_argument("--pipeline", choices=("all",) + PIPELINES, default="all")
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds to wait per pipeline")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        command = {"doctor": doctor, "plan": plan, "deploy": deploy, "status": status}[args.command]
        if args.command != "doctor":
            identity(args)
        command(args)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Started AWS executions continue; use status.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
