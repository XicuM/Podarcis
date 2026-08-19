import os
import re
import sys
import yaml

KNOWN_OKF_TYPES = {'concept', 'protocol', 'entity', 'overview', 'synthesis', 'guide', 'meta', 'recipe', 'journal'}

def strip_code_blocks(content):
    # Strip fenced code blocks
    content = re.sub(r'```[\s\S]*?```', '', content)
    # Strip inline code spans
    content = re.sub(r'`[^`\n]+`', '', content)
    return content

def find_markdown_links(content):
    # Standard markdown links [text](link)
    # Ignores external links, mailto, and anchors
    return re.findall(r'\[[^\]]+\]\(([^)]+)\)', content)

def check_footnotes(content, sources_ids=None):
    # Find all footnote definitions [^id]: starting at beginning of line
    defs = re.findall(r'^\s*\[\^([a-zA-Z0-9_-]+)\]:', content, re.MULTILINE)
    
    # Strip footnote definition lines before searching for references
    content_no_defs = re.sub(r'^\s*\[\^([a-zA-Z0-9_-]+)\]:.*$', '', content, flags=re.MULTILINE)
    refs = re.findall(r'\[\^([a-zA-Z0-9_-]+)\]', content_no_defs)
    
    missing_defs = [r for r in refs if r not in defs]
    unused_defs = [d for d in defs if d not in refs]
    
    # Positional/numeric labels are forbidden (OKF §5.1): labels must be stable ids, not [^1]
    positional = sorted({r for r in refs if re.fullmatch(r'\d+', r)})
    
    unmatched_sources = []
    if sources_ids is not None:
        for ref in refs:
            if ref not in sources_ids and ref not in defs:
                unmatched_sources.append(ref)
                
    return missing_defs, unused_defs, unmatched_sources, positional

def try_fix_unquoted_colons(fm_text):
    """Attempt to fix unquoted colons in single-line YAML values like 'title: Protocol: Social Life'."""
    lines = fm_text.splitlines()
    fixed_lines = []
    changed = False
    for line in lines:
        match = re.match(r'^(\s*[\w_-]+\s*:\s*)([^"\'\s].*:\s*.*)$', line)
        if match and not line.strip().startswith('#'):
            prefix, val = match.groups()
            # Quote the value safely if it's not already wrapped in quotes
            if not (val.startswith('"') and val.endswith('"')) and not (val.startswith("'") and val.endswith("'")):
                fixed_val = val.replace('"', '\\"')
                fixed_lines.append(f'{prefix}"{fixed_val}"')
                changed = True
                continue
        fixed_lines.append(line)
    return "\n".join(fixed_lines), changed

def check_yaml_file(filepath):
    """Validate standalone .yaml or .yml file syntax."""
    results = {"yaml_errors": []}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        yaml.safe_load(content)
    except yaml.YAMLError as ye:
        mark = getattr(ye, 'problem_mark', None)
        if mark:
            results["yaml_errors"].append(f"Invalid YAML syntax at line {mark.line + 1}, column {mark.column + 1}: {ye.problem}")
        else:
            results["yaml_errors"].append(f"Invalid YAML syntax: {str(ye)}")
    except Exception as e:
        results["yaml_errors"].append(f"Could not read YAML file: {str(e)}")
    return results

def check_file(filepath, do_fix=False):
    results = {
        "broken_links": [],
        "missing_footnotes": [],
        "unused_footnotes": [],
        "unmatched_sources": [],
        "positional_footnotes": [],
        "missing_frontmatter": [],
        "yaml_errors": [],
        "word_count": 0
    }
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return {"error": str(e)}
    
    content_no_code = strip_code_blocks(content)

    # Check Frontmatter (OKF v0.2)
    parts = os.path.normpath(filepath).split(os.sep)
    is_wiki_or_workspace = 'wiki' in parts or 'workspace' in parts or 'user' in parts
    filename = os.path.basename(filepath)
    sources_ids = []

    if filename not in ['index.md', '_index.md'] and is_wiki_or_workspace:
        fm_match = re.match(r'^\s*---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if not fm_match:
            results["missing_frontmatter"].append("Entire YAML frontmatter block is missing")
        else:
            fm_text = fm_match.group(1)
            fm_data = None
            try:
                fm_data = yaml.safe_load(fm_text) or {}
            except yaml.YAMLError as ye:
                # Try auto-repairing unquoted colons if requested or for graceful evaluation
                fixed_fm_text, was_fixed = try_fix_unquoted_colons(fm_text)
                if was_fixed:
                    try:
                        fm_data = yaml.safe_load(fixed_fm_text) or {}
                        if do_fix:
                            # Apply fix to content
                            new_content = content[:fm_match.start(1)] + fixed_fm_text + content[fm_match.end(1):]
                            with open(filepath, 'w', encoding='utf-8') as f:
                                f.write(new_content)
                            results["missing_frontmatter"].append("[FIXED] Auto-quoted value containing colons in frontmatter")
                    except Exception:
                        fm_data = None
                
                if fm_data is None:
                    mark = getattr(ye, 'problem_mark', None)
                    if mark:
                        results["yaml_errors"].append(f"Invalid YAML frontmatter at line {mark.line + 1}, column {mark.column + 1}: {ye.problem}")
                    else:
                        results["yaml_errors"].append(f"Invalid YAML frontmatter: {str(ye)}")

            if isinstance(fm_data, dict):
                missing = []
                if "type" not in fm_data:
                    missing.append("type")
                if "category" not in fm_data:
                    missing.append("category")
                if "rationale" not in fm_data:
                    missing.append("rationale")

                if missing:
                    results["missing_frontmatter"].append(f"Missing required fields: {', '.join(missing)}")

                # Check type schema if present
                doc_type = str(fm_data.get("type", "")).lower()
                if doc_type and doc_type not in KNOWN_OKF_TYPES:
                    results["yaml_errors"].append(f"Unknown OKF document type '{doc_type}' (expected one of: {', '.join(sorted(KNOWN_OKF_TYPES))})")

                # Extract source IDs
                sources = fm_data.get("sources", [])
                if isinstance(sources, list):
                    for src in sources:
                        if isinstance(src, dict) and "id" in src:
                            sources_ids.append(str(src["id"]))
                        elif isinstance(src, str):
                            sources_ids.append(src)
                elif sources is not None:
                    results["yaml_errors"].append("'sources' field in frontmatter must be a list")
            elif fm_data is not None:
                results["yaml_errors"].append(f"Frontmatter YAML must be a key-value mapping (got {type(fm_data).__name__})")

    # Check Links
    links = find_markdown_links(content_no_code)
    for link in links:
        if link.startswith(('http://', 'https://', '#', 'mailto:', 'gdrive:')):
            continue
        
        # Strip query params or anchors
        clean_link = link.split('#')[0].split('?')[0]
        if not clean_link:
            continue
            
        dir_path = os.path.dirname(filepath)
        target_path = os.path.normpath(os.path.join(dir_path, clean_link))
        
        if not os.path.exists(target_path):
            results["broken_links"].append((link, target_path))
            
    # Check Footnotes
    missing, unused, unmatched, positional = check_footnotes(content_no_code, sources_ids)
    results["missing_footnotes"] = missing
    results["unused_footnotes"] = unused
    results["unmatched_sources"] = unmatched
    results["positional_footnotes"] = positional
    
    # Check Word Count
    results["word_count"] = len(content.split())
            
    return results

def run_audit(target_path, do_fix=False):
    all_results = {}
    if os.path.isfile(target_path):
        if target_path.endswith(('.yaml', '.yml')):
            res = check_yaml_file(target_path)
            if res.get("yaml_errors"):
                all_results[target_path] = res
        elif target_path.endswith('.md'):
            res = check_file(target_path, do_fix=do_fix)
            if any(res.get(k) for k in ["broken_links", "missing_footnotes", "unused_footnotes", "unmatched_sources", "positional_footnotes", "missing_frontmatter", "yaml_errors"]):
                all_results[target_path] = res
        return all_results

    for root, dirs, files in os.walk(target_path):
        parts = os.path.normpath(root).split(os.sep)
        if any(ignored in parts for ignored in ['.git', '.venv', '.obsidian', '__pycache__', 'node_modules', 'tmp']):
            continue

        # Clean up flat recipe files if subdirectories exist
        if parts[-1] == 'recipes' and any(d in dirs for d in ['bowls', 'lunches', 'dinners']):
            for f in list(files):
                if f.endswith('.md') and f not in ['_index.md', 'index.md']:
                    flat_file = os.path.join(root, f)
                    try:
                        os.remove(flat_file)
                        files.remove(f)
                    except OSError:
                        pass
        
        # Check for directory bloat in wiki or workspace folder
        if 'wiki' in parts or 'workspace' in parts:
            items = [d for d in dirs if not d.startswith('.') and d != '__pycache__'] + \
                    [f for f in files if not f.startswith('.') and f not in ['index.md', '_index.md'] and not f.endswith('.pyc')]
            if len(items) > 15:
                all_results[root] = {"bloated_directory": len(items)}

        for file in files:
            path = os.path.join(root, file)
            if file.endswith(('.yaml', '.yml')) and not root.startswith(os.path.join(target_path, '.git')):
                res = check_yaml_file(path)
                if res.get("yaml_errors"):
                    all_results[path] = res
            elif file.endswith('.md'):
                if file == 'raw.md':
                    continue
                res = check_file(path, do_fix=do_fix)
                
                has_issues = False
                if res.get("broken_links") or res.get("missing_footnotes") or res.get("unused_footnotes") or res.get("positional_footnotes") or res.get("missing_frontmatter") or res.get("unmatched_sources") or res.get("yaml_errors"):
                    has_issues = True
                    
                parts = os.path.normpath(path).split(os.sep)
                if 'wiki' in parts or 'user' in parts:
                    if res.get("word_count", 0) > 1500:
                        has_issues = True
                
                if has_issues:
                    if path in all_results:
                        all_results[path].update(res)
                    else:
                        all_results[path] = res
    return all_results


if __name__ == "__main__":
    do_fix = "--fix" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--fix"]
    target = os.path.abspath(args[0] if args else ".")
    audit_results = run_audit(target, do_fix=do_fix)
    
    MAX_WORDS = 1500
    if not audit_results:
        print("Audit passed: No issues found.")
    else:
        for path, res in audit_results.items():
            rel = os.path.relpath(path, target)
            has_issues = False
            issues = []
            
            if res.get("bloated_directory"):
                issues.append(f"  - Bloated Directory: Contains {res['bloated_directory']} content files (limit is 15)")
                has_issues = True
            
            if "wiki" in path.split(os.sep) or "user" in path.split(os.sep):
                if res.get("word_count", 0) > MAX_WORDS:
                    issues.append(f"  - Page Length: {res['word_count']} words (limit is {MAX_WORDS})")
                    has_issues = True
                    
            if res.get("yaml_errors"):
                issues.append("YAML Syntax / Schema Errors:")
                for err in res["yaml_errors"]:
                    issues.append(f"  - {err}")
                has_issues = True

            if res.get("broken_links"):
                issues.append("Broken Links:")
                for link, broken_target in res["broken_links"]:
                    issues.append(f"  - {link} -> {broken_target}")
                has_issues = True
                
            if res.get("missing_footnotes"):
                issues.append(f"Missing Footnote Definitions: {', '.join(res['missing_footnotes'])}")
                has_issues = True
                
            if res.get("unused_footnotes"):
                issues.append(f"Unused Footnote Definitions: {', '.join(res['unused_footnotes'])}")
                has_issues = True

            if res.get("unmatched_sources"):
                issues.append(f"Unmatched Footnote Sources: {', '.join(res['unmatched_sources'])}")
                has_issues = True

            if res.get("positional_footnotes"):
                issues.append(f"Positional (numeric) footnotes — use named [^id] labels: {', '.join(res['positional_footnotes'])}")
                has_issues = True
                
            if res.get("missing_frontmatter"):
                issues.append(f"Missing Frontmatter: {'; '.join(res['missing_frontmatter'])}")
                has_issues = True
                
            if has_issues:
                print(f"\n--- {rel} ---")
                for issue in issues:
                    print(issue)

