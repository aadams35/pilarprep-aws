# Current AWS environment

Blue Mesa already runs production services on AWS. Workloads use multiple accounts, Amazon EKS for payment APIs, Amazon RDS for PostgreSQL for operational data, Amazon MSK for payment events, Amazon S3 for evidence archives, AWS KMS for encryption, AWS CloudTrail for audit history, and Amazon CloudWatch for monitoring.

The program should extend the current AWS operating model. It must not be framed as an on-premises-to-AWS migration. Architecture discovery should confirm account boundaries, network paths, deployment controls, and current recovery evidence.
