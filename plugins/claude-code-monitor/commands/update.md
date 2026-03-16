# /update-monitor

Update the claude-code-monitor plugin to the latest version.

## Instructions

Follow these steps in order:

### 1. Find the marketplace clone directory

The marketplace clone is located at `~/.claude/plugins/repos/`. Find the directory that contains this plugin's marketplace (look for folders containing `.claude-plugin/marketplace.json` with `claude-code-monitor` in the plugins list).

```bash
find ~/.claude/plugins/repos/ -name "marketplace.json" -path "*/.claude-plugin/*" 2>/dev/null
```

### 2. Pull the latest changes

`cd` into the marketplace clone directory and run `git pull` to fetch the latest version:

```bash
cd <marketplace-clone-dir> && git pull origin main
```

### 3. Run plugin update

```bash
claude plugins update claude-code-monitor
```

### 4. Verify the update

Read the updated `plugin.json` to confirm the new version:

```bash
cat <marketplace-clone-dir>/plugins/claude-code-monitor/.claude-plugin/plugin.json
```

### 5. Report results

Tell the user:
- The previous and new version numbers
- That they need to **restart their Claude Code session** for changes to take effect
- If the monitor overlay is running, they should close and relaunch it
