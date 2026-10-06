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

## Multi-step requests

- "Create a folder demo and put an nginx deployment YAML in it" → plan of 2 → `create_folder demo`, `create_file demo/nginx.yaml` + valid
- "make yamls for redis and postgres" → two `create_file`, both valid
- "Create nginx.yaml for the nginx image and then show me the file" → `create_file`, then the content
- "tell me a joke and create an nginx yaml" → the joke is refused, the file is still created
- "delete nginx.yaml and then create a redis yaml" → asks to confirm, lists what is queued → "yes" → `delete_file`, `create_file redis.yaml`; "no" → nothing, and says what was not done
- "now do the same for memcached and influxdb" (after a create) → repeats it with the same settings and folder
- "Create an nginx deployment with cpu 500m and memory 1Gi" → ONE file (two settings are not two requests)
- "create a deployment yaml for my web service" → asks which image → "postgres:16" → `create_file postgres.yaml`
