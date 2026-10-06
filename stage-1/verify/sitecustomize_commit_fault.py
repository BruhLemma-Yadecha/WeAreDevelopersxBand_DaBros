"""One-shot storage fault injection used only in the temporary assay copy."""

import os


_replace = os.replace
_marker = os.environ.get("FAIL_COMMIT_MARKER")


def replace_once_with_failure(source, destination):
    if _marker and os.path.exists(_marker):
        os.unlink(_marker)
        raise OSError("assay-injected os.replace failure")
    return _replace(source, destination)


os.replace = replace_once_with_failure
