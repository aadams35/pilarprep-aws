import json
import re
import unittest
from pathlib import Path


TEMPLATE_PATH = Path(__file__).resolve().parents[3] / "infrastructure" / "bedrock.yaml"
DEMO_SCENARIOS_PATH = Path(__file__).resolve().parents[3] / "data" / "demo-scenarios.json"


class TemplateSecurityTests(unittest.TestCase):
    def test_brief_roles_can_use_only_the_stack_data_key(self):
        template = TEMPLATE_PATH.read_text(encoding="utf-8")

        for policy_name in ("data-key-access", "worker-data-key-access"):
            policy_match = re.search(
                rf'PolicyName: !Sub "\$\{{ResourcePrefix\}}-{policy_name}"(?P<body>.*?)- !Ref "AWS::NoValue"',
                template,
                re.DOTALL,
            )
            self.assertIsNotNone(policy_match, f"Missing {policy_name} policy")
            policy = policy_match.group("body")
            for action in (
                "kms:Decrypt",
                "kms:DescribeKey",
                "kms:Encrypt",
                "kms:GenerateDataKey*",
                "kms:ReEncrypt*",
            ):
                self.assertIn(action, policy)
            self.assertIn("Resource: !GetAtt DataEncryptionKey.Arn", policy)
            self.assertNotIn('Resource: "*"', policy)

    def test_prompt_attack_filter_keeps_strict_input_protection(self):
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
        prompt_attack = re.search(
            r"- Type: PROMPT_ATTACK\s+InputStrength: (?P<input>\w+)\s+OutputStrength: (?P<output>\w+)",
            template,
        )

        self.assertIsNotNone(prompt_attack)
        self.assertEqual(prompt_attack.group("input"), "HIGH")
        self.assertEqual(prompt_attack.group("output"), "NONE")
        for policy_type in ("HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"):
            self.assertRegex(
                template,
                rf"- Type: {policy_type}\s+InputStrength: MEDIUM\s+OutputStrength: MEDIUM",
            )

    def test_blue_mesa_direction_is_factual_customer_context(self):
        scenarios = json.loads(DEMO_SCENARIOS_PATH.read_text(encoding="utf-8"))
        blue_mesa = next(item for item in scenarios if item["id"] == "bluemesa")
        direction = blue_mesa["additionalDirection"]

        self.assertIn("existing AWS customer", direction)
        self.assertIn("payroll integration", direction)
        self.assertNotIn("Treat BlueMesa", direction)
        self.assertNotIn("Make payroll", direction)


if __name__ == "__main__":
    unittest.main()
