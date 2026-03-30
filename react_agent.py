import argparse
import os
from pathlib import Path
import time
from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_anthropic import ChatAnthropic
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage

load_dotenv()

@tool
def write_file(file_path: str, content: str) -> str:
    """
    Writes the provided content (source code) to a file. 
    It will automatically create the subdirectories if they don't exist.
    """
    path = Path(file_path).resolve()
    # Sandbox: ensure the file is inside the output directory
    if hasattr(write_file, '_output_dir'):
        output_dir = write_file._output_dir
        if not str(path).startswith(str(output_dir)):
            return f"Error: Path {file_path} is outside the output directory {output_dir}. Aborting."
    
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
        
    print(f"\n[Throttle] Successfully wrote {file_path}. Sleeping for 60 seconds to respect API rate limits...")
    time.sleep(60)
    
    return f"Success: Wrote {len(content)} characters to {file_path}"

def main():
    parser = argparse.ArgumentParser(description="React UI Generation Agent from PRD")
    parser.add_argument("--prd", required=True, help="Path to the PRD markdown document")
    parser.add_argument("--output", default="./react_output", help="Directory where the React app will be generated")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Anthropic Model (Sonnet is recommended for complex coding tasks)")
    parser.add_argument("--skill", default="REACT_SKILL.md", help="Path to the React skill specification file (default: REACT_SKILL.md)")
    args = parser.parse_args()
    
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set in .env")
        return

    prd_path = Path(args.prd)
    if not prd_path.exists():
        print(f"Error: Could not find PRD document at {args.prd}")
        return
        
    with open(prd_path, "r", encoding="utf-8") as f:
        prd_content = f.read()

    output_dir = Path(args.output).absolute()
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Sandbox the write_file tool to only write inside output_dir
    write_file._output_dir = output_dir

    # Extract component name from PRD filename for a generic prompt
    component_name = prd_path.stem.replace('_PRD', '').replace('_prd', '')

    print(f"=================================================")
    print(f" Starting React Builder Agent")
    print(f" Target PRD: {prd_path.name}")
    print(f" Component:  {component_name}")
    print(f" Output Dir: {output_dir}")
    print(f" Model Used: {args.model}")
    print(f"=================================================\n")
    
    # Load skill specification
    skill_path = Path(args.skill)
    if skill_path.exists():
        with open(skill_path, "r", encoding="utf-8") as f:
            skill_content = f.read()
        print(f" Skill File: {skill_path.name}")
    else:
        print(f" [Warning] Skill file not found at '{args.skill}' — using minimal prompt.")
        skill_content = ""

    # Initialize LLM & Tool binding
    llm = ChatAnthropic(model=args.model, temperature=0.1)
    tools = [write_file]
    
    system_prompt = f"""You are an expert React/TypeScript frontend architect agent.
Your objective is to read a Product Requirements Document (PRD) mapped from a legacy WPF application and completely rewrite the UI into an enterprise-ready React application.

You have access to a `write_file` tool. Use it autonomously. All files MUST be written INSIDE: {output_dir}

{'=' * 60}
SKILL SPECIFICATION — Follow every rule below exactly:
{'=' * 60}

{skill_content}

{'=' * 60}
END OF SKILL SPECIFICATION
{'=' * 60}

Do not ask for permission. Proactively call `write_file` until every file in the Implementation Checklist above is created.
"""

    agent = create_react_agent(llm, tools=tools, prompt=system_prompt)

    prompt = (
        f"Here is the detailed Product Requirements Document (PRD) for the '{component_name}' component:\n\n"
        f"--- START PRD ---\n"
        f"{prd_content}\n"
        f"--- END PRD ---\n\n"
        f"Analyze this PRD carefully and use the `write_file` tool to implement the COMPLETE React application. "
        f"Follow every rule in the Skill Specification provided in your system prompt. "
        f"All file paths must start with '{output_dir}/src/'. "
        f"Work through the Implementation Checklist in order and do not stop until every item is checked off."
    )

    print("Sending PRD to Claude (this will take a few minutes as it writes multiple files locally)...")
    
    try:
        inputs = {"messages": [HumanMessage(content=prompt)]}
        # stream_mode="values" returns all new messages added to the graph sequence
        for chunk in agent.stream(inputs, stream_mode="values"):
            msg = chunk["messages"][-1]
            # Print Claude's thought process
            if msg.content and getattr(msg, "type", "") == "ai":
                print(f"Agent Action: {msg.content}")
                
            # Print Tool Invocations
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for t in msg.tool_calls:
                    print(f"  --> [Creating File] {t['args'].get('file_path')}")
                    
        print("\n[SUCCESS] React generation complete! Check the output directory.")
    except Exception as e:
        print(f"Error during agent execution: {e}")

if __name__ == "__main__":
    main()
