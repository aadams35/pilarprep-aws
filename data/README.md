# Synthetic Demo Data

`demo-scenarios.json` defines the fictional customer scenarios used in local examples and evaluation. `blue-mesa-evidence/` contains the bounded retrieval corpus and its metadata sidecars. `brief-quality-rubric.json` defines offline regression expectations.

BlueMesa Payments already operates on AWS. Its example focuses on a bounded payroll-partner integration, not an initial migration from on-premises infrastructure. The source material includes both decision-makers and stakeholders so the briefing can identify names, positions, priorities, and open questions.

`blue-mesa-meeting-script.json` is the synthetic recording script. The corresponding audio is [blue-mesa-discovery.mp3](../demo-assets/blue-mesa-discovery.mp3). The audio is selected and uploaded by the user; it is not automatically injected into a meeting.

The [preparation script](../scripts/prepare-blue-mesa-rag.ps1) can publish this evidence to an authorized demo Knowledge Base and regenerate demo speech using Amazon Polly. That operation uses AWS and may incur charges. It is not part of CI or local verification.

Do not replace these fixtures with actual customer information in a public repository.
