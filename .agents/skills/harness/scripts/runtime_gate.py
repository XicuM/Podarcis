import sys
import os
import urllib.parse

# List of pre-approved domains for literature searches and standard APIs
APPROVED_DOMAINS = {
    "api.semanticscholar.org",
    "arxiv.org",
    "export.arxiv.org",
    "api.openalex.org",
    "eutils.ncbi.nlm.nih.gov",
    "www.googleapis.com",
    "api.unpaywall.org",
    "www.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov"
}

def check_permission(action_type: str, details: str) -> bool:
    """
    Enforces authorization check for system and network operations.
    Raises PermissionError if the action is denied or fails safe.
    """
    if action_type == "network":
        # Parse host/domain from URL details
        try:
            parsed_url = urllib.parse.urlparse(details)
            domain = parsed_url.netloc.split(":")[0]  # strip port if present
        except Exception:
            raise PermissionError(f"Gating Error: Invalid URL details: {details}")

        if domain in APPROVED_DOMAINS: return True

        if os.environ.get("HARNESS_BYPASS_GATE") == "1":
            print(f"⚠️  [Permission Gate] Bypass active. Allowing network request: {details}")
            return True

        prompt = f"\n⚠️  [Permission Gate] Script is attempting to access an unapproved domain:\nDomain: {domain} ({details})\nAllow this request? (y/n): "

        if not sys.stdin.isatty():
            raise PermissionError(f"Permission Denied: Network request to '{domain}' blocked in non-interactive environment: {details}")

        try:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            response = sys.stdin.readline().strip().lower()
            if response in ("y", "yes"):
                return True
            else:
                raise PermissionError("Permission Denied: Network request refused by user.")
        except Exception as e:
            if isinstance(e, PermissionError):
                raise
            raise PermissionError("Permission Gate Error: Failed to prompt user. Network request blocked.")


    elif action_type == "shell":
        if os.environ.get("HARNESS_BYPASS_GATE") == "1":
            print(f"⚠️  [Permission Gate] Bypass active. Allowing shell execution: {details}")
            return True

        # Check command execution
        prompt = f"\n⚠️  [Permission Gate] Script is attempting to execute a shell command:\nCommand: {details}\nAllow this execution? (y/n): "
        
        if not sys.stdin.isatty():
            raise PermissionError(f"Permission Denied: Shell execution blocked in non-interactive environment: {details}")

        try:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            response = sys.stdin.readline().strip().lower()
            if response in ("y", "yes"): return True
            else: raise PermissionError(f"Permission Denied: Shell command execution refused by user.")
        except Exception as e:
            if isinstance(e, PermissionError): raise
            raise PermissionError(f"Permission Gate Error: Failed to prompt user. Shell execution blocked.")
            
    else:
        raise PermissionError(f"Gating Error: Unknown action type: {action_type}")
