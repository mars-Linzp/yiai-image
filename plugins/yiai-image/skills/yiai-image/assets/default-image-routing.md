## Default Image Generation and Editing

For image generation or image editing requests, automatically load and use the
`yiai-image` (YiAI-image) skill as the default image workflow. This includes ordinary
requests to draw, generate artwork or visual assets, edit existing images,
change backgrounds, or recolor images. The user does not need to name the skill,
provider, or model. Respect an explicit request for another provider or workflow.

When the skill has no API key, ask the user for their AigcRouter user API key
directly in the conversation. Save the supplied key using the skill's internal
setup procedure and continue the pending request. Do not ask the user to run
scripts, open a terminal, or set environment variables. Reuse the saved key for
subsequent requests. Default to `https://api.aigcrouter.org` and image model
`gpt-image-2.5-sunburst` unless the user requests otherwise.

Reading SKILL.md is preparation. Continue with the client's status check, run
the requested generation or edit when credentials are available, wait for the
result, and display the saved image using an absolute path. Finish with an image,
a missing-key question, or a concrete error; do not end with an empty response.
If higher-priority host instructions require another image path, explain that
conflict. Do not claim that loading the skill means its HTTP client was used.
