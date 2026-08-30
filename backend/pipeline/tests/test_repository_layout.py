"""Check that the source tree and packaged Lambda handlers agree."""

import ast
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[3]


class TemplateLoader(yaml.SafeLoader):
    pass


def cloudformation_value(loader, tag, node):
    if isinstance(node, yaml.ScalarNode):
        return {tag: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {tag: loader.construct_sequence(node)}
    return {tag: loader.construct_mapping(node)}


TemplateLoader.add_multi_constructor("!", cloudformation_value)


class RepositoryLayoutTests(unittest.TestCase):
    def test_all_lambda_code_paths_and_handler_symbols_exist(self):
        checked = 0
        for template_path in sorted((ROOT / "infrastructure").glob("*.yaml")):
            template = yaml.load(template_path.read_text(encoding="utf-8"), Loader=TemplateLoader)
            for name, resource in template.get("Resources", {}).items():
                if resource["Type"] != "AWS::Serverless::Function":
                    continue
                properties = resource["Properties"]
                code_root = (template_path.parent / properties["CodeUri"]).resolve()
                self.assertTrue(code_root.is_relative_to(ROOT), name)
                module, symbol = properties["Handler"].rsplit(".", 1)
                source = code_root.joinpath(*module.split(".")).with_suffix(".py")
                self.assertTrue(source.is_file(), f"{name}: handler module missing")
                tree = ast.parse(source.read_text(encoding="utf-8"))
                functions = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
                self.assertIn(symbol, functions, f"{name}: handler function missing")
                checked += 1
        self.assertEqual(checked, 7)

    def test_shared_api_and_worker_use_the_architecture_entry_points(self):
        template = yaml.load((ROOT / "infrastructure" / "jobs-pipeline.yaml").read_text(encoding="utf-8"), Loader=TemplateLoader)
        resources = template["Resources"]
        self.assertEqual(resources["JobsApiFunction"]["Properties"]["Handler"], "jobs_api.handler.handler")
        self.assertEqual(resources["AiWorkerFunction"]["Properties"]["Handler"], "ai_worker.handler.handler")

    def test_runtime_packaging_and_diagram_remain_present(self):
        script = (ROOT / "scripts" / "deploy-agentcore.ps1").read_text(encoding="utf-8")
        self.assertIn('"infrastructure\\agentcore.yaml"', script)
        for path in ("runtime/main.py", "runtime/service.py", "runtime/meeting.py", "runtime/evidence.py", "tools/handler.py"):
            self.assertTrue((ROOT / "backend" / "agentcore" / path).is_file(), path)
        self.assertTrue((ROOT / "docs" / "architecture" / "pilarprep-aws-architecture.png").is_file())
