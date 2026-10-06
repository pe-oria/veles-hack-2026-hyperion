# Acceptance prompts

Run these in the IDE (or with curl) before every push.

- "What is HyperAI?" / "What are Open Connectors?" / "What is a DeviceNode?" → grounded answers
- "What is the weather today?" / "Write me a poem about pizza" → refusal
- "Create a folder called demo" → `create_folder`
- "Create a deployment YAML for a service using the nginx Docker image" → `create_file` + valid
- "Change the memory to 2Gi" (follow-up, no file named) → uses session `last_file`, `edit_file`
- "Delete it" → asks confirmation → "yes" → `delete_file`
- "Delete it" → "no" → nothing happens; "Delete it" → "What is HyperAI?" → not deleted, question answered
- Same create request twice → second one asks to overwrite → "yes" → `edit_file` + valid
- "delete the demo folder" → asks confirmation (lists known files inside) → "yes" → `delete_folder`
- "Validate nginx.yaml" → summarises `validate_file` report
