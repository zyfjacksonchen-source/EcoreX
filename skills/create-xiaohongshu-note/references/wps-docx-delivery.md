# WPS DOCX Delivery

Use WPS only after the user approves the final note package.

Default WPS root:

`C:\EcoreX Artifact Desk\cli-anything-wps-master`

The delivery script writes a WPS Writer project JSON and then runs:

```powershell
python -m cli_anything.wps --project <project.json> export render <output.docx> -p docx --overwrite
```

Only render a final DOCX after the complete note package has a produced cover image. If cover generation is blocked, render a draft/status document only when the user asks for it and label it incomplete.

The DOCX should include:

- Title page with brand, topic, and date.
- Final cover image.
- Optional inner pages only when requested and produced, using the same design language as the cover.
- Title candidates and selected title.
- Body copy.
- Tags.
- First comment.
- Formula decomposition and Audit Master self-check.
- Asset manifest.

If WPS Office, COM automation, or pywin32 is unavailable, do not fake success. Keep the project JSON and report the preflight/export error.
