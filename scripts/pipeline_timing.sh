#!/usr/bin/env bash
# Per-action timing and estimated cost of the latest successful ecg-backend pipeline executions.
# Usage: scripts/pipeline_timing.sh [N=8]   (needs the aws CLI and python3; region/profile from the environment)
# Prices (sa-east-1 on-demand, measured before the move to us-east-1; us-east-1 is cheaper, re-check): CodeBuild ARM small US$0.00525/min (billed per started minute),
# CodePipeline V2 US$0.002/action-minute. Fargate one-off tasks and the overlapping service task are not included
# (US$0.000566/min each for 0.5 vCPU / 1 GB: Fargate ARM US$0.0557/vCPU-h + US$0.00612/GB-h).
set -euo pipefail
N="${1:-8}"
export AWS_PAGER=""
aws codepipeline list-pipeline-executions --pipeline-name ecg-backend --max-results 50 \
  --query 'pipelineExecutionSummaries[?status==`Succeeded`].[pipelineExecutionId,startTime,lastUpdateTime]' --output json |
python3 -c '
import json, math, subprocess, sys
from datetime import datetime
n = int(sys.argv[1])
CB, CP = 0.00525, 0.002
rows = []
for eid, start, end in json.load(sys.stdin)[:n]:
    out = subprocess.run(["aws", "codepipeline", "list-action-executions", "--pipeline-name", "ecg-backend",
                          "--filter", f"pipelineExecutionId={eid}", "--output", "json"],
                         capture_output=True, text=True, check=True).stdout
    secs, cost = {}, 0.0
    for a in json.loads(out)["actionExecutionDetails"]:
        if a["stageName"] == "Source":
            continue
        d = (datetime.fromisoformat(a["lastUpdateTime"]) - datetime.fromisoformat(a["startTime"])).total_seconds()
        secs[a["actionName"]] = d
        cost += math.ceil(d / 60) * (CB + CP)
    total = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    rows.append((start[:16], total, secs, cost))
    print(f"{start[:16]}  total {total/60:5.1f} min  " +
          "  ".join(f"{k} {v:4.0f}s" for k, v in sorted(secs.items())) + f"  ~US${cost:.3f}")
if rows:
    print(f"average total: {sum(r[1] for r in rows)/len(rows)/60:.1f} min over {len(rows)} runs")
' "$N"
