#!/bin/bash

# Porto Installation Script - Installs Porto (PRD Decomposition System) for Claude Code
# Supports Windows (Git Bash, MSYS2, Cygwin), macOS, and Linux

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Detect OS
detect_os() {
    if [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "win32" ]] || [[ "$OSTYPE" == "cygwin" ]]; then
        echo "windows"
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    else
        echo "linux"
    fi
}

OS_TYPE=$(detect_os)

# Default language
SKILL_LANG="en"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --lang=*)
            SKILL_LANG="${1#*=}"
            shift
            ;;
        --lang)
            SKILL_LANG="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --lang=LANG, --lang LANG    Set language (en or zhcn, default: en)"
            echo "  -h, --help                  Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                  # Use default language (en)"
            echo "  $0 --lang=zhcn      # Use Chinese"
            echo "  $0 --lang en        # Use English"
            exit 0
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Validate language code
if [[ "$SKILL_LANG" != "en" && "$SKILL_LANG" != "zhcn" && "$SKILL_LANG" != "zh-cn" ]]; then
    echo -e "${RED}Error: Unsupported language '$SKILL_LANG'${NC}"
    echo "Supported languages: en, zhcn"
    exit 1
fi

# Normalize zh-cn to zhcn
if [[ "$SKILL_LANG" == "zh-cn" ]]; then
    SKILL_LANG="zhcn"
fi

# Get the absolute path of this script's directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -f "$SCRIPT_DIR/config.example.json" ]; then
    echo -e "${RED}Please run this script from the Porto project root directory${NC}"
    exit 1
fi

# Source directories
PORTO_SKILL_SRC="$SCRIPT_DIR/skills/$SKILL_LANG"
PORTO_AGENT_SRC="$SCRIPT_DIR/agents/$SKILL_LANG/prd-analyst.md"

# Target directories
CLAUDE_DIR="$HOME/.claude"
CLAUDE_SKILLS_DIR="$CLAUDE_DIR/skills"
CLAUDE_AGENTS_DIR="$CLAUDE_DIR/agents"
SKILL_TARGET_DIR="$CLAUDE_SKILLS_DIR/porto"
PORTO_DATA_DIR="$HOME/.porto"

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}  Porto (PRD Decomposition) Setup${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${GREEN}OS Detected:${NC} $OS_TYPE"
echo -e "${GREEN}Language:${NC} $SKILL_LANG"
echo -e "${GREEN}Source:${NC}"
echo "  Skill: $PORTO_SKILL_SRC"
echo "  Agent: $PORTO_AGENT_SRC"
echo -e "${GREEN}Target:${NC}"
echo "  Skill: $SKILL_TARGET_DIR"
echo "  Agent: $CLAUDE_AGENTS_DIR/prd-analyst.md"
echo "  Data: $PORTO_DATA_DIR"
echo ""

# Windows-specific warnings
if [ "$OS_TYPE" = "windows" ]; then
    echo -e "${YELLOW}⚠ Windows Detected${NC}"
    echo -e "${YELLOW}Note: On Windows, files will be copied instead of linked.${NC}"
    echo -e "${YELLOW}You'll need to re-run this script after making changes to skills.${NC}"
    echo ""
fi

# Validate source directories
if [ ! -d "$PORTO_SKILL_SRC" ]; then
    echo -e "${RED}Error: Skill not found: $PORTO_SKILL_SRC${NC}"
    echo "Available languages:"
    ls -d "$SCRIPT_DIR/skills/"*/ 2>/dev/null | xargs -n 1 basename
    exit 1
fi

if [ ! -f "$PORTO_AGENT_SRC" ]; then
    echo -e "${RED}Error: Agent not found: $PORTO_AGENT_SRC${NC}"
    echo "Available languages:"
    ls -d "$SCRIPT_DIR/agents/"*/ 2>/dev/null | xargs -n 1 basename
    exit 1
fi

# Function to create directory if it doesn't exist
create_dir() {
    local dir=$1
    if [ ! -d "$dir" ]; then
        echo -e "${YELLOW}Creating directory:${NC} $dir"
        mkdir -p "$dir"
    else
        echo -e "${GREEN}Directory exists:${NC} $dir"
    fi
}

# Function to create symlink (Unix/Mac) or copy (Windows)
create_symlink() {
    local source=$1
    local target=$2
    local name=$3

    if [ "$OS_TYPE" = "windows" ]; then
        # Windows: always remove and re-copy to ensure latest version
        if [ -e "$target" ]; then
            echo -e "${YELLOW}⟳${NC} $name (updating)"
            rm -rf "$target"
        else
            echo -e "${GREEN}+${NC} $name (copying)"
        fi
        if [ -d "$source" ]; then
            cp -r "$source" "$target"
        else
            cp "$source" "$target"
        fi
        echo -e "${GREEN}  Copied${NC}"
    else
        # Unix/Mac: use symbolic links
        if [ -L "$target" ]; then
            local current_target
            current_target=$(readlink "$target")
            if [ "$current_target" = "$source" ]; then
                echo -e "${GREEN}✓${NC} $name (already linked)"
            else
                echo -e "${YELLOW}⟳${NC} $name (updating link)"
                rm "$target"
                ln -s "$source" "$target"
            fi
        elif [ -e "$target" ]; then
            echo -e "${RED}✗${NC} $name (conflict: $target exists and is not a symlink)"
            echo -e "${YELLOW}  Please manually remove or backup:${NC} $target"
            return 1
        else
            echo -e "${GREEN}+${NC} $name (creating link)"
            ln -s "$source" "$target"
        fi
    fi
}

# Step 1: Create Porto data directory
echo -e "\n${BLUE}Step 1: Creating Porto data directory${NC}"
create_dir "$PORTO_DATA_DIR"
create_dir "$PORTO_DATA_DIR/workflows"

# Copy config if not exists
if [ -f "$PORTO_DATA_DIR/config.json" ]; then
    echo -e "${GREEN}config.json already exists, skipping${NC}"
else
    cp "$SCRIPT_DIR/config.example.json" "$PORTO_DATA_DIR/config.json"
    echo -e "${GREEN}+${NC} config.json (copied)"
fi

# Install server.py (always overwrite to get latest version)
cp "$SCRIPT_DIR/porto_server.py" "$PORTO_DATA_DIR/server.py"
echo -e "${GREEN}+${NC} server.py (copied)"

# Step 2: Create Claude directories
echo -e "\n${BLUE}Step 2: Creating Claude directories${NC}"
create_dir "$CLAUDE_DIR"
create_dir "$CLAUDE_SKILLS_DIR"
create_dir "$CLAUDE_AGENTS_DIR"

# Step 3: Install porto skill
echo -e "\n${BLUE}Step 3: Installing porto skill ($SKILL_LANG)${NC}"

# Remove old skill structure if exists (flat files from previous install)
for old_skill in \
    "$CLAUDE_SKILLS_DIR"/prd-decomposition.md \
    "$CLAUDE_SKILLS_DIR"/subsystem-identification.md \
    "$CLAUDE_SKILLS_DIR"/subsystem-context-generation.md \
    "$CLAUDE_SKILLS_DIR"/subsystem-specification.md \
    "$CLAUDE_SKILLS_DIR"/subsystem-spec-generation.md \
    "$CLAUDE_SKILLS_DIR"/knowledge-retrieval.md; do
    if [ -e "$old_skill" ] || [ -L "$old_skill" ]; then
        echo -e "${YELLOW}⟳ Removing old flat skill file: $(basename "$old_skill")${NC}"
        rm -f "$old_skill"
    fi
done

# Remove old skill directory if exists and is not a symlink
if [ -d "$SKILL_TARGET_DIR" ] && [ ! -L "$SKILL_TARGET_DIR" ]; then
    echo -e "${YELLOW}⟳ Removing old skill directory...${NC}"
    rm -rf "$SKILL_TARGET_DIR"
fi

# Link/copy skill content file by file (to allow references to coexist)
create_dir "$SKILL_TARGET_DIR"

shopt -s nullglob
skill_files=("$PORTO_SKILL_SRC"/*)
shopt -u nullglob

for skill_file in "${skill_files[@]}"; do
    file_name=$(basename "$skill_file")
    # Skip references directory - handle separately
    if [ "$file_name" = "references" ]; then
        continue
    fi
    create_symlink "$skill_file" "$SKILL_TARGET_DIR/$file_name" "porto/$file_name"
done

# Step 4: Install references directory
echo -e "\n${BLUE}Step 4: Installing references${NC}"
if [ -d "$PORTO_SKILL_SRC/references" ]; then
    create_symlink "$PORTO_SKILL_SRC/references" "$SKILL_TARGET_DIR/references" "porto/references"
else
    echo -e "${YELLOW}No references directory found, skipping${NC}"
fi

# Step 5: Install prd-analyst agent
echo -e "\n${BLUE}Step 5: Installing prd-analyst agent ($SKILL_LANG)${NC}"
create_symlink "$PORTO_AGENT_SRC" "$CLAUDE_AGENTS_DIR/prd-analyst.md" "prd-analyst.md"

# Step 6: Clean up old command symlinks (from previous install format)
echo -e "\n${BLUE}Step 6: Cleaning up old command files${NC}"
COMMANDS_DIR="$CLAUDE_DIR/commands"
if [ -d "$COMMANDS_DIR" ]; then
    for old_cmd in "$COMMANDS_DIR"/porto.*.md; do
        if [ -e "$old_cmd" ] || [ -L "$old_cmd" ]; then
            echo -e "${YELLOW}⟳ Removing old command: $(basename "$old_cmd")${NC}"
            rm -f "$old_cmd"
        fi
    done
fi

# Step 7: Verification
echo -e "\n${BLUE}Step 7: Verification${NC}"

echo -e "\n${YELLOW}porto skill (~/.claude/skills/porto/):${NC}"
if [ -d "$SKILL_TARGET_DIR" ]; then
    ls -lh "$SKILL_TARGET_DIR" | tail -n +2 | awk '{print "  " $NF}'
else
    echo -e "${RED}  skill directory not found${NC}"
fi

echo -e "\n${YELLOW}prd-analyst agent:${NC}"
if [ -L "$CLAUDE_AGENTS_DIR/prd-analyst.md" ]; then
    echo -e "${GREEN}  ✓ linked${NC}"
    ls -lh "$CLAUDE_AGENTS_DIR/prd-analyst.md" | awk '{print "  " $9 " -> " $11}'
elif [ -f "$CLAUDE_AGENTS_DIR/prd-analyst.md" ]; then
    echo -e "${GREEN}  ✓ installed (Windows copy)${NC}"
else
    echo -e "${RED}  ✗ not installed${NC}"
fi

echo -e "\n${YELLOW}references directory:${NC}"
if [ -d "$SKILL_TARGET_DIR/references" ]; then
    ls "$SKILL_TARGET_DIR/references" | while read line; do echo "  $line"; done
else
    echo -e "${RED}  references not found${NC}"
fi

echo -e "\n${YELLOW}knowledge bases:${NC}"
if [ -f "$HOME/.porto/config.json" ]; then
    KB_COUNT=$(python3 -c "import json; cfg=json.load(open('$HOME/.porto/config.json')); kbs=[k for k in cfg.get('knowledge_bases',[]) if k.get('enabled',True)]; print(len(kbs))" 2>/dev/null || echo "0")
    echo -e "${GREEN}  ✓ $KB_COUNT knowledge base(s) configured${NC}"
else
    echo -e "${YELLOW}  ⚠ no config found (optional)${NC}"
fi

# Step 8: Summary
echo -e "\n${BLUE}========================================${NC}"
echo -e "${GREEN}Porto installation complete!${NC}"
echo -e "${BLUE}========================================${NC}"
echo ""
echo -e "${GREEN}Configuration:${NC}"
echo "  Language: $SKILL_LANG"
echo "  Data directory: $PORTO_DATA_DIR"
echo "  Skill: $SKILL_TARGET_DIR"
echo "  Agent: $CLAUDE_AGENTS_DIR/prd-analyst.md"
echo ""
echo -e "${GREEN}You can now use Porto commands in Claude Code:${NC}"
echo "  /porto gen <file_paths>     Start a new PRD decomposition workflow"
echo "  /porto continue             Continue to the next step"
echo "  /porto resume <workflow_id> Resume an interrupted workflow"
echo "  /porto status [workflow_id] View workflow status"
echo "  /porto list [options]       List all workflows"
echo ""
echo -e "${GREEN}To view workflows in browser:${NC}"
echo "  python3 ~/.porto/server.py"
echo "  Open http://127.0.0.1:8090"
echo ""
if [ "$OS_TYPE" = "windows" ]; then
    echo -e "${YELLOW}Windows Notes:${NC}"
    echo "  - Files are copied, not linked"
    echo "  - Re-run this script after updating skills"
    echo "  - To remove: rm -rf $PORTO_DATA_DIR $SKILL_TARGET_DIR $CLAUDE_AGENTS_DIR/prd-analyst.md"
else
    echo -e "${GREEN}Notes:${NC}"
    echo "  - Changes to skills/agents in this project are immediately reflected"
    echo "  - To switch language, run: $0 --lang=<en|zhcn>"
    echo "  - To remove: rm -rf $PORTO_DATA_DIR $SKILL_TARGET_DIR $CLAUDE_AGENTS_DIR/prd-analyst.md"
fi
echo ""
echo -e "${GREEN}For more information, see README.md${NC}"
