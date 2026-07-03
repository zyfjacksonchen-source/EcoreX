# S1 Config Safety

## Intent

Remove high-risk config loading behavior before building more runtime automation on top of it.

## Implemented Changes

- Environment variable overrides no longer use `eval`.
- Overrides are parsed by the declared `available_setting` value type:
  - booleans accept `true/false/1/0/yes/no/on/off`;
  - numbers parse as `int` or `float`;
  - lists and dicts parse through JSON or `ast.literal_eval`;
  - strings remain strings.
- User data is saved to `user_datas.json` instead of `user_datas.pkl`.
- Legacy `user_datas.pkl` migration uses a restricted unpickler that rejects global/class references and size-limits the file.

## Acceptance

- A malicious environment override must not execute code.
- JSON user data load/save must round-trip.
- A malicious legacy pickle must be rejected and must not execute code.
- A safe legacy pickle containing only primitive containers may migrate to JSON.
