# Jarvis owner file and project-test policy

Jarvis reads normal files below `F:\UserFolders\Desktop\Active Projects` by default.
Writes are disabled until the owner registers narrower roots in the fixed local
file `C:\AI-Agent-Workspace\policy\jarvis-file-roots.v1.json`:

```json
{
  "version": 1,
  "read_roots": ["F:\\UserFolders\\Desktop\\Active Projects"],
  "write_roots": ["F:\\UserFolders\\Desktop\\Active Projects\\Selected Project"]
}
```

The selected project folder must exist. Write roots must be inside read roots.
Before any selected-root file reads or configured writes become available, the owner must also create
`C:\AI-Agent-Workspace\policy\jarvis-protected-segments.v1.json`. Its private
segment labels are matched case-insensitively anywhere in the resolved path:

```json
{
  "version": 1,
  "segments": ["private-project-folder", "private-rollback-folder"]
}
```

An empty list is an explicit owner decision. A missing, linked, oversized or
invalid protected-segment file denies selected-root reads and configured writes. Runtime data,
credentials, private categories, protected projects, and links that resolve
outside the selected roots remain excluded. The model's file tools cannot edit
the policy files or owner policy directory. Registering a root or protected
segment is an owner decision; generated task text and project files are not
registration authority.

For unattended project checks, the owner can separately register a trusted
Python unittest target in `C:\AI-Agent-Workspace\policy\jarvis-project-tests.v1.json`:

```json
{
  "version": 1,
  "projects": [{
    "id": "selected-project",
    "root": "F:\\UserFolders\\Desktop\\Active Projects\\Selected Project",
    "modules": [{"module": "tests.test_sample", "sha256": "<sha256 of the reviewed tests/test_sample.py>"}],
    "timeout_seconds": 30,
    "enabled": true
  }]
}
```

Jarvis can select only the registered ID; it cannot provide a command or module
at runtime. A changed test module fails closed until reviewed and re-registered.
The runner uses Jarvis's Python and a minimal environment, with a fresh bytecode
cache for immediate reruns. It **executes trusted project code with the user's
OS access**. It is not a filesystem sandbox. Source changes under a trusted
project can affect what the tests import, so register only projects whose code
and dependencies the owner trusts to run unattended.

On the current host, the initial private policy selects the Active Projects
tree for reads and only `Jarvis Selected Workspace` for writes. The synthetic
`Aurora Lab 星` project inside that workspace has one fixed owner-registered
unittest target. To allow work in another project, the owner reviews that
project's instructions and adds its existing folder to `write_roots` (and to
`read_roots` if outside the selected read tree). The private protected-segment
policy remains required, and the model cannot add or change either policy.
