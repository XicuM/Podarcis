import os
import re
import sys

def find_markdown_links(content):
    # Standard markdown links [text](link)
    # Ignores external links, mailto, and anchors
    return re.findall(r'\[[^\]]+\]\(([^)]+)\)', content)

def check_footnotes(content):
    # Find all footnote references [^1]
    refs = re.findall(r'\[\^([a-zA-Z0-9]+)\](?!:)', content)
    # Find all footnote definitions [^1]:
    defs = re.findall(r'\[\^([a-zA-Z0-9]+)\]:', content)
    
    missing_defs = [r for r in refs if r not in defs]
    unused_defs = [d for d in defs if d not in refs]
    
    return missing_defs, unused_defs

def check_file(filepath):
    results = {
        "broken_links": [],
        "missing_footnotes": [],
        "unused_footnotes": [],
        "missing_frontmatter": [],
        "word_count": 0
    }
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return {"error": str(e)}
    
    # Check Links
    links = find_markdown_links(content)
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
    missing, unused = check_footnotes(content)
    results["missing_footnotes"] = missing
    results["unused_footnotes"] = unused
    
    # Check Frontmatter
    parts = os.path.normpath(filepath).split(os.sep)
    is_wiki_or_workspace = 'wiki' in parts or 'workspace' in parts or 'user' in parts
    if os.path.basename(filepath) != '_index.md' and is_wiki_or_workspace:
        fm_match = re.match(r'^\s*---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if not fm_match:
            results["missing_frontmatter"].append("Entire YAML frontmatter block is missing")
        else:
            fm_text = fm_match.group(1)
            has_category = re.search(r'^category\s*:', fm_text, re.MULTILINE)
            has_related = re.search(r'^related\s*:', fm_text, re.MULTILINE)
            has_rationale = re.search(r'^rationale\s*:', fm_text, re.MULTILINE)
            
            missing = []
            if not has_category:
                missing.append("category")
            if not has_related:
                missing.append("related")
            if not has_rationale:
                missing.append("rationale")
                
            if missing:
                results["missing_frontmatter"].append(f"Missing required fields: {', '.join(missing)}")
    
    # Check Word Count
    results["word_count"] = len(content.split())
            
    return results

def run_audit(root_dir):
    all_results = {}
    for root, dirs, files in os.walk(root_dir):
        if any(ignored in root for ignored in ['.git', '.venv', '.obsidian', '__pycache__']):
            continue
        
        # Check for directory bloat in wiki or workspace folder
        parts = os.path.normpath(root).split(os.sep)
        if 'wiki' in parts or 'workspace' in parts:
            # Count immediate children, excluding _index.md and hidden files/dirs
            items = [d for d in dirs if not d.startswith('.') and d != '__pycache__'] + \
                    [f for f in files if not f.startswith('.') and f != '_index.md' and not f.endswith('.pyc')]
            if len(items) > 15:
                all_results[root] = {"bloated_directory": len(items)}

        for file in files:
            if file.endswith('.md'):
                path = os.path.join(root, file)
                res = check_file(path)
                
                has_issues = False
                if res.get("broken_links") or res.get("missing_footnotes") or res.get("unused_footnotes") or res.get("missing_frontmatter"):
                    has_issues = True
                    
                parts = os.path.normpath(path).split(os.sep)
                if 'wiki' in parts or 'user' in parts:
                    if res.get("word_count", 0) > 1500:
                        has_issues = True
                
                if has_issues:
                    # Merge with existing directory check results if any
                    if path in all_results:
                        all_results[path].update(res)
                    else:
                        all_results[path] = res
    return all_results

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "."
    audit_results = run_audit(target)
    
    MAX_WORDS = 1500
    if not audit_results:
        print("Audit passed: No issues found.")
    else:
        for path, res in audit_results.items():
            # Check if there are actual issues to report for this file
            has_issues = False
            issues = []
            
            if res.get("bloated_directory"):
                issues.append(f"  - Bloated Directory: Contains {res['bloated_directory']} content files (limit is 15)")
                has_issues = True
            
            if "wiki" in path.split(os.sep) or "user" in path.split(os.sep):
                if res.get("word_count", 0) > MAX_WORDS:
                    issues.append(f"  - Page Length: {res['word_count']} words (limit is {MAX_WORDS})")
                    has_issues = True
                    
            if res.get("broken_links"):
                issues.append("Broken Links:")
                for link, target in res["broken_links"]:
                    issues.append(f"  - {link} -> {target}")
                has_issues = True
                
            if res.get("missing_footnotes"):
                issues.append(f"Missing Footnote Definitions: {', '.join(res['missing_footnotes'])}")
                has_issues = True
                
            if res.get("unused_footnotes"):
                issues.append(f"Unused Footnote Definitions: {', '.join(res['unused_footnotes'])}")
                has_issues = True
                
            if res.get("missing_frontmatter"):
                issues.append(f"Missing Frontmatter: {'; '.join(res['missing_frontmatter'])}")
                has_issues = True
                
            if has_issues:
                print(f"\n--- {path} ---")
                for issue in issues:
                    print(issue)
