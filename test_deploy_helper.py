"""Offline checks for account guard, exact execution tracking and failure handling."""
import argparse
import unittest
from unittest.mock import patch

import deploy


class DeploymentTests(unittest.TestCase):
    def args(self, **kwargs):
        return argparse.Namespace(profile=None, pipeline="ecg-backend", timeout=30, **kwargs)

    def test_wrong_account_blocks_actions(self):
        with patch.object(deploy, "aws", return_value={"Account": "other", "Arn": "test"}):
            with self.assertRaisesRegex(RuntimeError, "Expected account"):
                deploy.identity(self.args())

    def test_waits_for_started_execution(self):
        replies = [{"pipelineExecutionSummaries": []}, {"pipelineExecutionId": "exact-id"},
                   {"pipelineExecution": {"status": "InProgress"}},
                   {"pipelineExecution": {"status": "Succeeded"}}]
        with patch.object(deploy, "aws", side_effect=replies) as aws, \
                patch.object(deploy.time, "sleep"), patch.object(deploy, "health") as health:
            deploy.deploy(self.args())
            self.assertEqual(aws.call_args.args[-1], "exact-id")
            health.assert_called_once()

    def test_failure_does_not_report_health_success(self):
        replies = [{"pipelineExecutionSummaries": []}, {"pipelineExecutionId": "id"},
                   {"pipelineExecution": {"status": "Failed"}}]
        with patch.object(deploy, "aws", side_effect=replies), patch.object(deploy, "health") as health:
            with self.assertRaisesRegex(RuntimeError, "Failed"):
                deploy.deploy(self.args())
            health.assert_not_called()

    def test_active_execution_blocks_duplicate(self):
        with patch.object(deploy, "aws", return_value={
            "pipelineExecutionSummaries": [{"status": "InProgress"}]
        }) as aws:
            with self.assertRaisesRegex(RuntimeError, "already has an active"):
                deploy.deploy(self.args())
            self.assertEqual(aws.call_count, 1)

    def test_empty_aws_response(self):
        with patch.object(deploy, "run", return_value=""):
            self.assertEqual(deploy.aws(self.args(), "s3api", "head-bucket"), {})

    def test_doctor_reports_missing_local_tool_before_aws_identity(self):
        with patch.object(deploy.shutil, "which", return_value=None), \
                patch.object(deploy, "identity") as identity:
            with self.assertRaisesRegex(RuntimeError, "Install aws"):
                deploy.doctor(self.args())
            identity.assert_not_called()

    def test_plan_preserves_profile(self):
        args = self.args()
        args.profile = "competition"
        with patch.object(deploy.shutil, "which", return_value="terraform"), \
                patch.object(deploy.subprocess, "run") as run:
            run.return_value.returncode = 0
            deploy.plan(args)
            command = run.call_args.args[0]
            self.assertIn("-var=aws_profile=competition", command)
            self.assertEqual(run.call_args.kwargs["env"]["AWS_PROFILE"], "competition")
            self.assertNotIn("apply", command)

    def test_timeout_leaves_execution_id_in_error(self):
        replies = [{"pipelineExecutionSummaries": []}, {"pipelineExecutionId": "exact-id"}]
        with patch.object(deploy, "aws", side_effect=replies), \
                patch.object(deploy.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaisesRegex(RuntimeError, "exact-id.*AWS continues"):
                deploy.deploy(self.args())


if __name__ == "__main__":
    unittest.main()
