---
name: yurich
description: Open the YURICH local project search and quick-edit interface when the user asks to open YURICH or work with files through YURICH.
---

# YURICH

When the user asks to open or use YURICH, call the `open_yurich` tool exactly
once and present its returned interactive interface.

Do not open the `ui://yurich/...` resource directly and do not use MCP resource
listing as a substitute. A directly opened resource does not receive the full
interactive tool bridge, which breaks actions such as **Browse…**, search, file
editing, and display-mode switching.

Do not inspect the current project or run shell commands merely to launch the
interface. Opening YURICH alone is not authorization to read or modify project
files outside the user's actions in the interface.

If `open_yurich` is unavailable, explain that the plugin was not loaded into
the current task and ask the user to open a new task after installation.
