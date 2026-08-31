"""Staged, retain-and-import migration of PilarPrep's physical storage names.

Read-only inventory is the default. Mutations require --apply and a specific phase.
Snapshots and object manifests stay in ignored work/resource-names, never in Git.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import boto3
import yaml
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "work" / "resource-names"
STACKS = {
    "core": "pillarprep-bedrock",
    "jobs": "pillarprep-jobs",
    "agent": "pillarprep-agentcore",
    "frontend": "pillarprep-frontend",
}
FILES = {"core": "bedrock.yaml", "jobs": "jobs-pipeline.yaml",
         "agent": "agentcore.yaml", "frontend": "frontend.yaml"}
BUCKETS = {
    "core": ("BriefArtifactsBucket", "ArtifactBucketName", "artifacts"),
    "jobs": ("MeetingEvidenceBucket", "MeetingEvidenceBucketName", "meeting-evidence"),
    "frontend": ("FrontendBucket", "FrontendBucketName", "web-assets"),
}
NEW_PARAMETERS = {
    "core": ["ArtifactBucketName"],
    "jobs": ["MeetingEvidenceBucketName", "EvidenceVectorBucketName"],
    "frontend": ["CloudFrontName"],
    "agent": [],
}
NEW_CONDITIONS = {
    "core": ["HasArtifactBucketName"],
    "jobs": ["HasMeetingEvidenceBucketName", "HasEvidenceVectorBucketName"],
    "frontend": ["HasCloudFrontName"],
    "agent": [],
}
TERMINAL_JOBS = {"complete", "completed", "failed", "cancelled", "review-ready", "approved"}
MALWARE_TAG = "GuardDutyMalwareScanStatus"


class CfnLoader(yaml.SafeLoader):
    pass


def cfn_tag(loader, tag, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    name = tag if tag in {"Ref", "Condition"} else "Fn::" + tag
    if tag == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {name: value}


CfnLoader.add_multi_constructor("!", cfn_tag)


def load_template(value):
    return yaml.load(value, Loader=CfnLoader) if isinstance(value, str) else copy.deepcopy(value)


def resource_name(purpose, account, region, environment="demo"):
    value = f"pilarprep-{environment}-{purpose}-{account}-{region}".lower()
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", value):
        raise ValueError("Invalid account or storage name")
    return value


def replace_strings(value, replacements):
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [replace_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: replace_strings(item, replacements) for key, item in value.items()}
    return value


def literal_bucket_references(value, logical_id, bucket, region):
    """Resolve only this bucket's references while it is detached for import."""
    attributes = {
        "Arn": f"arn:aws:s3:::{bucket}",
        "DomainName": f"{bucket}.s3.amazonaws.com",
        "RegionalDomainName": f"{bucket}.s3.{region}.amazonaws.com",
        "DualStackDomainName": f"{bucket}.s3.dualstack.{region}.amazonaws.com",
    }
    if isinstance(value, dict):
        if value == {"Ref": logical_id}:
            return bucket
        att = value.get("Fn::GetAtt")
        if isinstance(att, str):
            att = att.split(".", 1)
        if isinstance(att, list) and len(att) == 2 and att[0] == logical_id:
            if att[1] not in attributes:
                raise ValueError(f"Unsupported bucket attribute: {att[1]}")
            return attributes[att[1]]
        if "Fn::Sub" in value:
            substitutions = {"${" + logical_id + "}": bucket}
            substitutions.update({"${" + logical_id + "." + key + "}": item for key, item in attributes.items()})
            value = replace_strings(value, substitutions)
        return {key: literal_bucket_references(item, logical_id, bucket, region) for key, item in value.items()}
    if isinstance(value, list):
        return [literal_bucket_references(item, logical_id, bucket, region) for item in value]
    return value


def naming_template(kind, live):
    """Apply only naming/retention edits to the live packaged template."""
    result = copy.deepcopy(live)
    local = load_template((ROOT / "infrastructure" / FILES[kind]).read_text(encoding="utf-8-sig"))
    for key in NEW_PARAMETERS[kind]:
        result.setdefault("Parameters", {})[key] = local["Parameters"][key]
    for key in NEW_CONDITIONS[kind]:
        result.setdefault("Conditions", {})[key] = local["Conditions"][key]
    for key, resource in result["Resources"].items():
        local_resource = local["Resources"].get(key, {})
        if resource["Type"] in {
            "AWS::S3::Bucket", "AWS::S3::BucketPolicy", "AWS::S3Vectors::VectorBucket",
            "AWS::S3Vectors::Index", "AWS::S3Vectors::VectorBucketPolicy",
            "AWS::Bedrock::KnowledgeBase", "AWS::Bedrock::DataSource",
        }:
            resource["DeletionPolicy"] = "Retain"
            resource["UpdateReplacePolicy"] = "Retain"
        if key in {"BriefArtifactsBucket", "MeetingEvidenceBucket", "FrontendBucket", "BlueMesaVectorBucket"}:
            props = resource["Properties"]
            local_props = local_resource["Properties"]
            name_key = "VectorBucketName" if resource["Type"].startswith("AWS::S3Vectors") else "BucketName"
            props[name_key] = local_props[name_key]
            props["Tags"] = local_props["Tags"]
        if resource["Type"] == "AWS::CloudFront::Distribution":
            resource["Properties"]["DistributionConfig"]["Comment"] = local_resource["Properties"]["DistributionConfig"]["Comment"]
            resource["Properties"]["Tags"] = local_resource["Properties"]["Tags"]
    return result


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def response_missing(exc):
    return exc.response.get("Error", {}).get("Code") in {
        "404", "NoSuchBucket", "NoSuchBucketPolicy", "NoSuchCORSConfiguration",
        "NoSuchLifecycleConfiguration", "OwnershipControlsNotFoundError", "NoSuchTagSet",
    }


def public_summary(state):
    return {
        "account": state["account"], "region": state["region"],
        "targetBucketNames": state["targets"],
        "targetCloudFrontName": f"pilarprep-{state['environment']}-web",
        "completedPhases": state["steps"],
        "distributionId": state["stacks"]["frontend"]["outputs"]["CloudFrontDistributionId"],
        "websiteUnchanged": "https://pilarprep.app",
    }


class Migration:
    def __init__(self, args):
        self.args = args
        self.session = boto3.Session(profile_name=args.profile, region_name=args.region)
        self.config = Config(connect_timeout=10, read_timeout=90, retries={"mode": "standard", "max_attempts": 3})
        identity = self.client("sts").get_caller_identity()
        if ":assumed-role/PilarPrepHackathonDeployer/" not in identity["Arn"]:
            raise PermissionError("Use the PilarPrepHackathonDeployer role, not root or another project.")
        self.account = identity["Account"]
        if args.account_id and args.account_id != self.account:
            raise PermissionError("The authenticated AWS account does not match --account-id.")
        self.cf = self.client("cloudformation")
        self.s3 = self.client("s3")
        self.state_path = WORK / "migration-state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        if self.state and (self.state["account"] != self.account or self.state["region"] != args.region
                           or self.state["environment"] != args.environment):
            raise PermissionError("The saved migration belongs to another account or region.")

    def client(self, service):
        return self.session.client(service, config=self.config)

    def stack(self, kind):
        return self.cf.describe_stacks(StackName=STACKS[kind])["Stacks"][0]

    def template(self, kind):
        return load_template(self.cf.get_template(StackName=STACKS[kind], TemplateStage="Original")["TemplateBody"])

    def outputs(self, kind):
        return {item["OutputKey"]: item["OutputValue"] for item in self.stack(kind).get("Outputs", [])}

    def require_apply(self):
        if not self.args.apply:
            raise ValueError("This phase changes AWS. Review inventory and supply --apply explicitly.")

    def save(self):
        save_json(self.state_path, self.state)

    def inventory(self):
        stacks = {}
        for kind in STACKS:
            stack = self.stack(kind)
            if stack["StackStatus"] not in {"CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"}:
                raise RuntimeError(f"{STACKS[kind]} is not ready: {stack['StackStatus']}")
            stacks[kind] = {
                "parameters": {item["ParameterKey"]: item.get("ParameterValue", "") for item in stack.get("Parameters", [])},
                "outputs": {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])},
                "template": self.template(kind),
            }
        sources = {
            "artifacts": stacks["core"]["outputs"]["ArtifactBucketName"],
            "meeting-evidence": stacks["jobs"]["outputs"]["MeetingEvidenceBucketName"],
            "web-assets": stacks["frontend"]["outputs"]["FrontendBucketName"],
            "deployments": stacks["agent"]["parameters"]["RuntimeCodeBucket"],
        }
        return {
            "account": self.account, "region": self.args.region, "environment": self.args.environment,
            "createdAt": datetime.now().isoformat(), "stacks": stacks, "sources": sources,
            "targets": {purpose: resource_name(purpose, self.account, self.args.region, self.args.environment)
                        for purpose in [*sources, "evidence-vectors"]},
            "copies": {}, "steps": [],
        }

    def log(self, message):
        print(message, flush=True)

    def object_map(self, bucket):
        result = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, ExpectedBucketOwner=self.account):
            for item in page.get("Contents", []):
                result[item["Key"]] = {"etag": item["ETag"], "size": item["Size"]}
        return result

    def clone_bucket(self, purpose):
        source, target = self.state["sources"][purpose], self.state["targets"][purpose]
        if source == target:
            return
        exists = False
        try:
            self.s3.head_bucket(Bucket=target, ExpectedBucketOwner=self.account)
            exists = True
        except ClientError as exc:
            if not response_missing(exc):
                raise
        if exists:
            tags = {tag["Key"]: tag["Value"] for tag in self.s3.get_bucket_tagging(Bucket=target)["TagSet"]}
            if tags.get("MigrationSource") != source:
                raise RuntimeError("Destination already exists without this migration's ownership marker.")
        else:
            arguments = {"Bucket": target, "ObjectOwnership": "BucketOwnerEnforced"}
            if self.args.region != "us-east-1":
                arguments["CreateBucketConfiguration"] = {"LocationConstraint": self.args.region}
            self.s3.create_bucket(**arguments)
            self.s3.put_bucket_tagging(Bucket=target, Tagging={"TagSet": [{"Key": "MigrationSource", "Value": source}]})
        self.s3.put_public_access_block(Bucket=target, PublicAccessBlockConfiguration={
            key: True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
        })
        encryption = self.s3.get_bucket_encryption(Bucket=source)["ServerSideEncryptionConfiguration"]
        self.s3.put_bucket_encryption(Bucket=target, ServerSideEncryptionConfiguration=encryption)
        self.s3.put_bucket_versioning(Bucket=target, VersioningConfiguration={"Status": "Enabled"})
        for getter, putter, response_key, argument_key in [
            ("get_bucket_cors", "put_bucket_cors", "CORSRules", "CORSConfiguration"),
            ("get_bucket_lifecycle_configuration", "put_bucket_lifecycle_configuration", "Rules", "LifecycleConfiguration"),
        ]:
            try:
                value = getattr(self.s3, getter)(Bucket=source)[response_key]
                getattr(self.s3, putter)(Bucket=target, **{argument_key: {response_key: value}})
            except ClientError as exc:
                if not response_missing(exc):
                    raise
        try:
            policy = json.loads(self.s3.get_bucket_policy(Bucket=source)["Policy"])
            policy = replace_strings(policy, {source: target})
        except ClientError as exc:
            if not response_missing(exc):
                raise
            policy = {"Version": "2012-10-17", "Statement": [{
                "Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
                "Resource": [f"arn:aws:s3:::{target}", f"arn:aws:s3:::{target}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            }]}
        self.s3.put_bucket_policy(Bucket=target, Policy=json.dumps(policy))
        try:
            tags = {item["Key"]: item["Value"] for item in self.s3.get_bucket_tagging(Bucket=source)["TagSet"]
                    if not item["Key"].startswith("aws:")}
        except ClientError as exc:
            if not response_missing(exc):
                raise
            tags = {}
        tags.update(Name=f"pilarprep-{self.args.environment}-{purpose}", Project="PilarPrep",
                    Repository="aadams35/pilarprep-aws", MigrationSource=source)
        if purpose == "deployments":
            tags["ManagedBy"] = "deployment-scripts"
        self.s3.put_bucket_tagging(Bucket=target, Tagging={"TagSet": [{"Key": key, "Value": value} for key, value in tags.items()]})
        self.log(f"Prepared private bucket: {target}")

    def copy_objects(self, purpose, *, audio_keys=None):
        source, target = self.state["sources"][purpose], self.state["targets"][purpose]
        source_items, target_items = self.object_map(source), self.object_map(target)
        prior = self.state["copies"].setdefault(purpose, {})
        copied = verified = 0
        for key, value in source_items.items():
            is_upload = purpose == "meeting-evidence" and key.startswith("audio/uploads/")
            if is_upload and audio_keys is None:
                continue
            if audio_keys is not None and key not in audio_keys:
                continue
            if key == "malware-protection-resource-validation-object":
                continue
            old_copy = prior.get(key)
            if old_copy and old_copy["source"] == value and key in target_items:
                if target_items[key] == old_copy["target"]:
                    verified += 1
                    continue
                raise RuntimeError("Destination changed after migration copy; refusing to overwrite application writes.")
            if key in target_items and not old_copy:
                raise RuntimeError("Destination contains an untracked object; refusing to overwrite it.")
            head = self.s3.head_object(Bucket=source, Key=key, ExpectedBucketOwner=self.account)
            version = head.get("VersionId")
            copy_source = {"Bucket": source, "Key": key}
            if version:
                copy_source["VersionId"] = version
            tags_args = {"Bucket": source, "Key": key}
            if version:
                tags_args["VersionId"] = version
            tags = self.s3.get_object_tagging(**tags_args)["TagSet"]
            tags = [tag for tag in tags if tag["Key"] != MALWARE_TAG]
            response = self.s3.copy_object(
                Bucket=target, Key=key, CopySource=copy_source, CopySourceIfMatch=value["etag"],
                ExpectedBucketOwner=self.account, ExpectedSourceBucketOwner=self.account,
                MetadataDirective="COPY", TaggingDirective="REPLACE",
                Tagging=urlencode({tag["Key"]: tag["Value"] for tag in tags}),
                ChecksumAlgorithm="SHA256",
            )
            new_head = self.s3.head_object(Bucket=target, Key=key)
            if new_head["ContentLength"] != value["size"]:
                raise RuntimeError("Copied object length does not match the source.")
            prior[key] = {"source": value, "target": {"etag": new_head["ETag"], "size": new_head["ContentLength"]},
                          "sourceVersion": version, "targetVersion": response.get("VersionId")}
            copied += 1
            self.save()
            if copied % 20 == 0:
                self.log(f"{purpose}: copied {copied} objects")
        # Mirror deletions only for objects this migration created and nobody subsequently modified.
        for key, record in list(prior.items()):
            if key in source_items or key not in target_items:
                continue
            if purpose == "deployments":
                continue
            if target_items[key] != record["target"]:
                raise RuntimeError("A deleted source has a newer destination write; manual reconciliation is required.")
            self.s3.delete_object(Bucket=target, Key=key, ExpectedBucketOwner=self.account)
            del prior[key]
        self.save()
        self.log(f"{purpose}: {copied} copied, {verified} previously verified; original versions retained")

    def parameters(self, kind, template, overrides):
        current = {item["ParameterKey"] for item in self.stack(kind).get("Parameters", [])}
        values = []
        for key, definition in template.get("Parameters", {}).items():
            if key in overrides:
                values.append({"ParameterKey": key, "ParameterValue": str(overrides[key])})
            elif key in current:
                values.append({"ParameterKey": key, "UsePreviousValue": True})
            elif "Default" not in definition:
                raise ValueError(f"Missing required parameter: {key}")
        return values

    def deploy_template(self, kind, template, overrides=None, *, imports=None, allowed_removals=()):
        self.require_apply()
        overrides = overrides or {}
        stack_name = STACKS[kind]
        identifier = "resource-names-" + str(time.time_ns())
        body = json.dumps(template, default=str)
        request = {
            "StackName": stack_name, "ChangeSetName": identifier,
            "ChangeSetType": "IMPORT" if imports else "UPDATE",
            "Capabilities": ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"],
            "Parameters": self.parameters(kind, template, overrides),
        }
        if len(body.encode()) <= 51200:
            request["TemplateBody"] = body
        else:
            bucket = self.state["sources"]["deployments"]
            key = "resource-name-migration/templates/" + identifier + ".json"
            self.s3.put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="application/json")
            request["TemplateURL"] = f"https://{bucket}.s3.{self.args.region}.amazonaws.com/{key}"
        if imports:
            request["ResourcesToImport"] = imports
        self.cf.validate_template(**{key: request[key] for key in ("TemplateBody", "TemplateURL") if key in request})
        self.cf.create_change_set(**request)
        while True:
            change = self.cf.describe_change_set(StackName=stack_name, ChangeSetName=identifier)
            if change["Status"] in {"CREATE_COMPLETE", "FAILED"}:
                break
            time.sleep(3)
        if change["Status"] == "FAILED":
            reason = change.get("StatusReason", "")
            if "didn't contain changes" in reason or "No updates are to be performed" in reason:
                self.cf.delete_change_set(StackName=stack_name, ChangeSetName=identifier)
                self.log(f"{stack_name}: no configuration change")
                return
            raise RuntimeError(f"Change set rejected for {stack_name}: {reason}")
        changes = [item["ResourceChange"] for item in change.get("Changes", [])]
        for item in changes:
            if imports and item["Action"] != "Import":
                raise RuntimeError("Import would modify an existing resource; refusing to execute.")
            if item["Action"] == "Remove" and item["LogicalResourceId"] not in allowed_removals:
                raise RuntimeError(f"Unexpected resource removal: {item['LogicalResourceId']}")
            if item.get("Replacement") == "True" and item["ResourceType"] not in {
                "AWS::S3::BucketPolicy", "AWS::S3Vectors::VectorBucket", "AWS::S3Vectors::Index",
                "AWS::S3Vectors::VectorBucketPolicy", "AWS::Bedrock::KnowledgeBase", "AWS::Bedrock::DataSource",
                "AWS::Lambda::LayerVersion",
            }:
                raise RuntimeError(f"Unexpected replacement: {item['LogicalResourceId']}")
        self.log(f"{stack_name}: " + ", ".join(f"{item['Action']} {item['LogicalResourceId']}" for item in changes))
        previous_update = self.stack(kind).get("LastUpdatedTime")
        self.cf.execute_change_set(StackName=stack_name, ChangeSetName=identifier)
        saw_progress = False
        while True:
            current_stack = self.stack(kind)
            status = current_stack["StackStatus"]
            saw_progress = saw_progress or status.endswith("_IN_PROGRESS")
            updated = current_stack.get("LastUpdatedTime") != previous_update
            if status in {"UPDATE_COMPLETE", "IMPORT_COMPLETE"} and (saw_progress or updated):
                self.log(f"{stack_name}: {status}")
                return
            if "ROLLBACK" in status or status.endswith("_FAILED"):
                events = self.cf.describe_stack_events(StackName=stack_name)["StackEvents"]
                failures = [{"resource": item["LogicalResourceId"], "reason": item.get("ResourceStatusReason", "")}
                            for item in events if item["ResourceStatus"].endswith("_FAILED")][:3]
                raise RuntimeError(f"{stack_name}: {status}; {json.dumps(failures)}")
            time.sleep(10)

    def prepare(self):
        self.require_apply()
        if self.state and any(step.startswith("cutover-") for step in self.state["steps"]):
            raise RuntimeError("Preparation cannot be rerun after cutover; reconcile changes explicitly.")
        if not self.state:
            self.state = self.inventory()
            self.save()
        for purpose in self.state["sources"]:
            self.clone_bucket(purpose)
            self.copy_objects(purpose)
        self.state["steps"].append("prepare")
        self.save()

    def retain(self):
        self.require_apply()
        for kind in ("core", "jobs", "frontend"):
            self.deploy_template(kind, naming_template(kind, self.template(kind)))
        self.state["steps"].append("retain")
        self.save()

    def record_inventory(self):
        ddb = self.client("dynamodb")
        table = self.outputs("core")["ProjectStateTableName"]
        fields = ["projectId", "sortKey", "entityType", "status", "expiresAt", "updatedAt",
                  "objectKey", "expectedSizeBytes", "scanBucketName", "scanVersionId",
                  "scanETag", "scanTagVerified", "waitingJobId", "processingJobId"]
        names = {f"#f{index}": field for index, field in enumerate(fields)}
        records = []
        for page in ddb.get_paginator("scan").paginate(
            TableName=table, ProjectionExpression=", ".join(names),
            ExpressionAttributeNames=names, ConsistentRead=True,
        ):
            records.extend(page.get("Items", []))
        return table, records

    def pause(self):
        self.require_apply()
        if "resume" in self.state["steps"]:
            raise RuntimeError("This migration has already resumed processing.")
        if "pause" in self.state["steps"]:
            self.log("The original queue-consumer state is already recorded.")
            return
        jobs = self.outputs("jobs")
        queue = self.client("sqs").get_queue_attributes(
            QueueUrl=jobs["JobQueueUrl"],
            AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        if any(int(value) for value in queue.values()):
            raise RuntimeError("The queue is not idle. Let existing requests finish before cutover.")
        _, records = self.record_inventory()
        now = int(time.time())
        active = [item for item in records if item.get("entityType", {}).get("S") == "JOB"
                  and item.get("status", {}).get("S") not in TERMINAL_JOBS
                  and int(item.get("expiresAt", {}).get("N", now + 1)) > now]
        if active:
            raise RuntimeError(f"{len(active)} nonterminal job records remain; inspect them before cutover.")
        mappings = self.client("lambda").list_event_source_mappings(FunctionName=jobs["AiWorkerFunctionName"])["EventSourceMappings"]
        mappings = [item for item in mappings if item["EventSourceArn"].endswith(":pillarprep-demo-ai-jobs")]
        if len(mappings) != 1:
            raise RuntimeError("Expected exactly one PilarPrep jobs queue consumer.")
        self.state["mapping"] = {"uuid": mappings[0]["UUID"], "wasEnabled": mappings[0]["State"] == "Enabled"}
        self.save()
        self.client("lambda").update_event_source_mapping(UUID=mappings[0]["UUID"], Enabled=False)
        while self.client("lambda").get_event_source_mapping(UUID=mappings[0]["UUID"])["State"] != "Disabled":
            time.sleep(3)
        self.state["steps"].append("pause")
        self.save()
        self.log("Queue consumption paused; the application can keep queuing requests.")

    def paused_template(self, template):
        result = copy.deepcopy(template)
        result["Resources"]["AiWorkerFunction"]["Properties"]["Events"]["JobQueueEvent"]["Properties"]["Enabled"] = False
        return result

    def cutover(self, kind):
        self.require_apply()
        if "cutover-" + kind in self.state["steps"]:
            self.log(f"{kind}: cutover is already recorded; verify instead of repeating it.")
            return
        if kind != "frontend":
            mapping = self.state.get("mapping")
            if not mapping or self.client("lambda").get_event_source_mapping(UUID=mapping["uuid"])["State"] != "Disabled":
                raise RuntimeError("Pause the queue consumer before changing live data storage.")
        logical, parameter, purpose = BUCKETS[kind]
        target = self.state["targets"][purpose]
        live = self.template(kind)
        if logical in live["Resources"] and (
            live["Resources"][logical].get("DeletionPolicy") != "Retain"
            or live["Resources"][logical].get("UpdateReplacePolicy") != "Retain"
        ):
            raise RuntimeError("Apply and verify the retention phase before detaching the original bucket.")
        final = naming_template(kind, live)
        if logical not in final["Resources"]:
            saved = json.loads((WORK / f"{kind}-import-template.json").read_text())
            self.deploy_template(kind, saved, {parameter: target}, imports=[{
                "ResourceType": "AWS::S3::Bucket", "LogicalResourceId": logical,
                "ResourceIdentifier": {"BucketName": target},
            }])
            self.state["steps"].append("cutover-" + kind)
            self.save()
            return
        overrides = {parameter: target}
        if kind == "jobs":
            final = self.paused_template(final)
            overrides.update(ArtifactBucketName=self.state["targets"]["artifacts"],
                             EvidenceVectorBucketName=self.state["targets"]["evidence-vectors"],
                             KnowledgeBaseGeneration="v3")
        elif kind == "frontend":
            overrides["CloudFrontName"] = f"pilarprep-{self.args.environment}-web"
        self.copy_objects(purpose)
        save_json(WORK / f"{kind}-import-template.json", final)
        detached = copy.deepcopy(final)
        del detached["Resources"][logical]
        detached = literal_bucket_references(detached, logical, target, self.args.region)
        self.deploy_template(kind, detached, overrides, allowed_removals=[logical])
        self.deploy_template(kind, final, overrides, imports=[{
            "ResourceType": "AWS::S3::Bucket", "LogicalResourceId": logical,
            "ResourceIdentifier": {"BucketName": target},
        }])
        self.state["steps"].append("cutover-" + kind)
        self.save()

    def cutover_agent(self):
        self.require_apply()
        if "cutover-core" not in self.state["steps"] or "cutover-jobs" not in self.state["steps"]:
            raise RuntimeError("Migrate core and jobs storage before updating AgentCore.")
        mapping = self.state.get("mapping")
        if not mapping or self.client("lambda").get_event_source_mapping(UUID=mapping["uuid"])["State"] != "Disabled":
            raise RuntimeError("Keep the worker paused until all storage consumers agree.")
        jobs = self.outputs("jobs")
        self.deploy_template("agent", self.template("agent"), {
            "ArtifactBucketName": self.state["targets"]["artifacts"],
            "RuntimeCodeBucket": self.state["targets"]["deployments"],
            "LambdaDependencyBucket": self.state["targets"]["deployments"],
            "KnowledgeBaseId": jobs["KnowledgeBaseId"],
            "KnowledgeBaseArn": jobs["KnowledgeBaseArn"],
        })
        self.deploy_template("jobs", self.paused_template(self.template("jobs")), {
            "LambdaSdkLayerArn": self.outputs("agent")["AgentLambdaSdkLayerArn"],
        })
        self.state["steps"].append("cutover-agent")
        self.save()

    def reindex(self):
        self.require_apply()
        if "cutover-jobs" not in self.state["steps"]:
            raise RuntimeError("Migrate the evidence store before reindexing it.")
        jobs = self.outputs("jobs")
        bedrock = self.client("bedrock-agent")
        response = bedrock.start_ingestion_job(
            knowledgeBaseId=jobs["KnowledgeBaseId"], dataSourceId=jobs["KnowledgeBaseDataSourceId"],
            description="Reindex approved evidence after the retained storage-name migration.",
        )
        job_id = response["ingestionJob"]["ingestionJobId"]
        while True:
            job = bedrock.get_ingestion_job(
                knowledgeBaseId=jobs["KnowledgeBaseId"], dataSourceId=jobs["KnowledgeBaseDataSourceId"],
                ingestionJobId=job_id,
            )["ingestionJob"]
            if job["status"] in {"COMPLETE", "FAILED", "STOPPED"}:
                self.log("Evidence ingestion: " + job["status"] + " " + json.dumps(job.get("statistics", {})))
                if job["status"] != "COMPLETE" or job.get("statistics", {}).get("numberOfDocumentsFailed", 0):
                    raise RuntimeError("Evidence ingestion did not complete successfully.")
                self.state["steps"].append("reindex")
                self.save()
                return
            time.sleep(10)

    def rescan_audio(self):
        self.require_apply()
        if "cutover-jobs" not in self.state["steps"]:
            raise RuntimeError("Activate the new GuardDuty protection plan before copying audio uploads.")
        table, records = self.record_inventory()
        eligible = []
        now = int(time.time())
        source_items = self.object_map(self.state["sources"]["meeting-evidence"])
        for item in records:
            if item.get("entityType", {}).get("S") != "MEETING_AUDIO_UPLOAD":
                continue
            if int(item.get("expiresAt", {}).get("N", "0")) <= now:
                continue
            key = item.get("objectKey", {}).get("S", "")
            if key not in source_items:
                continue
            if item.get("status", {}).get("S") in {"blocked", "scan_failed"}:
                continue
            eligible.append(item)
        save_json(WORK / "audio-records-before-rescan.json", eligible)
        ddb = self.client("dynamodb")
        for item in eligible:
            key = {field: item[field] for field in ("projectId", "sortKey")}
            current = ddb.get_item(TableName=table, Key=key, ConsistentRead=True)["Item"]
            if current.get("scanBucketName", {}).get("S") == self.state["targets"]["meeting-evidence"]:
                continue
            ddb.update_item(
                TableName=table, Key=key,
                UpdateExpression="SET #status = :pending, scanTagVerified = :no REMOVE scanBucketName, scanVersionId, scanETag, scanResultStatus, scanEventId, waitingJobId, waitingInputKey, waitingInputVersion, waitingTraceId, processingJobId",
                ConditionExpression="#status = :previous AND objectKey = :key",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":pending": {"S": "pending_scan"}, ":no": {"BOOL": False},
                                           ":previous": current["status"], ":key": current["objectKey"]},
            )
        self.copy_objects("meeting-evidence", audio_keys={item["objectKey"]["S"] for item in eligible})
        self.state["audioRescanCount"] = len(eligible)
        self.state["steps"].append("rescan-audio")
        self.save()
        self.log(f"{len(eligible)} unexpired uploads queued for fresh GuardDuty scans; expired/unowned audio stays in the original private bucket.")

    def resume(self):
        self.require_apply()
        if "resume" in self.state["steps"]:
            self.log("Processing was already resumed; no objects were recopied.")
            return
        required = {"cutover-core", "cutover-jobs", "cutover-agent", "reindex", "rescan-audio"}
        if not required.issubset(set(self.state["steps"])):
            raise RuntimeError("Finish storage consumers, evidence ingestion, and audio rescan preparation before resuming.")
        self.copy_objects("artifacts")
        self.copy_objects("meeting-evidence")
        template = self.template("jobs")
        props = template["Resources"]["AiWorkerFunction"]["Properties"]["Events"]["JobQueueEvent"]["Properties"]
        props["Enabled"] = bool(self.state["mapping"]["wasEnabled"])
        self.deploy_template("jobs", template)
        self.state["steps"].append("resume")
        self.save()
        self.log("The unified worker is consuming queued work with the renamed storage.")

    def status(self):
        state = self.state or self.inventory()
        self.log(json.dumps(public_summary(state), indent=2))
        active = {}
        for kind in STACKS:
            stack = self.stack(kind)
            self.log(f"{STACKS[kind]}: {stack['StackStatus']}")
            outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
            if kind in BUCKETS:
                _, parameter, purpose = BUCKETS[kind]
                active[purpose] = outputs[parameter]
            if kind == "agent":
                parameters = {item["ParameterKey"]: item.get("ParameterValue", "")
                              for item in stack.get("Parameters", [])}
                active["deployments"] = parameters["RuntimeCodeBucket"]
            if kind == "jobs" and "EvidenceVectorBucketName" in outputs:
                active["evidence-vectors"] = outputs["EvidenceVectorBucketName"]
        self.log(json.dumps({"activeBucketNames": active}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["inventory", "prepare", "retain", "pause", "cutover-core",
                        "cutover-jobs", "cutover-agent", "cutover-frontend", "reindex", "rescan-audio",
                        "resume", "status"], nargs="?", default="inventory")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--profile", default="pillarprep-deployer")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--environment", default="demo")
    parser.add_argument("--account-id")
    args = parser.parse_args()
    migration = Migration(args)
    if args.phase == "inventory":
        print(json.dumps(public_summary(migration.inventory()), indent=2))
    elif args.phase.startswith("cutover-") and args.phase != "cutover-agent":
        migration.cutover(args.phase.removeprefix("cutover-"))
    else:
        getattr(migration, args.phase.replace("-", "_"))()


if __name__ == "__main__":
    main()
