# Licence texts for packages whose wheel ships none

A wheel on PyPI sometimes declares its licence in its metadata without including the text. `tools/build.py`
copies the file from here into the pack for such a package, and fails the build for any other package that
has no licence text at all.

| Package | File | From |
|---|---|---|
| flatbuffers 25.12.19 | `flatbuffers/LICENSE` (Apache-2.0) | https://github.com/google/flatbuffers/blob/v25.12.19/LICENSE |
