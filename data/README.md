# Synthetic Demo Data

Everything in this folder is fictional and safe to use in a public demonstration.

- `demo-scenarios.json` contains the customer scenarios shown in the application.
- `blue-mesa-evidence/` is the bounded evidence collection used by retrieval tests and the BlueMesa workflow.
- `brief-quality-rubric.json` defines the offline quality expectations.
- `blue-mesa-meeting-script.json` is the script behind the synthetic meeting recording.

BlueMesa Payments already runs its payment platform on AWS. The scenario is about adding a controlled payroll-partner integration, not moving an on-premises system to AWS. Its records include decision-makers and stakeholders so PilarPrep can return names, positions, priorities, and unresolved questions.

The corresponding recording is [blue-mesa-discovery.mp3](../demo-assets/blue-mesa-discovery.mp3). A user chooses and uploads it during the meeting workflow; the application does not inject it automatically.

[prepare-blue-mesa-rag.ps1](../scripts/prepare-blue-mesa-rag.ps1) can publish the evidence to an authorized demo Knowledge Base and regenerate the speech with Amazon Polly. That command uses AWS services and may incur charges, so it is not part of CI or offline verification.

Keep real customer information out of this public repository.
