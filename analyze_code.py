"""
Simple code analyzer for frontend projects
Generates insights about imports, dependencies, and code structure
"""
import json
import os
from pathlib import Path
from collections import defaultdict
import re

def analyze_codebase(target_dir):
    """Analyze a React/JS codebase and generate insights"""

    nodes = []
    edges = []
    imports_map = defaultdict(list)
    file_paths = []

    # Scan all JS/JSX files
    for root, dirs, files in os.walk(target_dir):
        # Skip node_modules and build folders
        if 'node_modules' in root or 'dist' in root or 'build' in root:
            continue

        for file in files:
            if file.endswith(('.js', '.jsx', '.ts', '.tsx')):
                file_path = os.path.join(root, file)
                rel_path = os.path.relpath(file_path, target_dir)
                file_paths.append(rel_path)

                # Add node
                nodes.append({
                    "id": rel_path,
                    "label": file,
                    "path": rel_path,
                    "type": "file"
                })

                # Parse imports
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    # Find imports
                    import_pattern = r"import.*from\s+['\"](.+?)['\"]"
                    matches = re.findall(import_pattern, content)

                    for imported in matches:
                        # Only track local imports (not node_modules)
                        if imported.startswith('.'):
                            imports_map[rel_path].append(imported)
                            edges.append({
                                "source": rel_path,
                                "target": imported,
                                "type": "imports"
                            })
                except Exception as e:
                    print(f"Error reading {file_path}: {e}")

    return {
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "total_files": len(nodes),
            "total_imports": len(edges),
            "scanned_directory": target_dir
        }
    }

if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "../admin-dashboard"
    output = sys.argv[2] if len(sys.argv) > 2 else "graphify_output.json"

    print(f"Analyzing {target}...")
    data = analyze_codebase(target)

    with open(output, 'w') as f:
        json.dump(data, f, indent=2)

    print(f"✅ Analysis complete: {output}")
    print(f"   Files: {data['metadata']['total_files']}")
    print(f"   Imports: {data['metadata']['total_imports']}")