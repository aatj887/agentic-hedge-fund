import asyncio
import os
import traceback
from typing import Any, Dict, Type
from dotenv import load_dotenv, find_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.agents import create_agent
from langchain_core.tools import StructuredTool, BaseTool
from pydantic import BaseModel, ConfigDict, Field, create_model

load_dotenv('.env')
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")

# 1. Schema Sanitization Logic
def sanitize_schema_for_google(schema_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    Recursively converts integer enums to strings and removes unsupported keys 
    (like 'additionalProperties') to make the schema compatible with Google GenAI.
    """
    if isinstance(schema_dict, dict):
        # Remove unsupported keys
        if 'additionalProperties' in schema_dict:
            del schema_dict['additionalProperties']
        
        # Fix Integer Enums: Convert [1, 2] to ["1", "2"] and set type to string
        if 'enum' in schema_dict:
            enum_values = schema_dict['enum']
            if any(isinstance(v, int) for v in enum_values):
                schema_dict['enum'] = [str(v) for v in enum_values]
                schema_dict['type'] = 'string'  # Force type to string
        
        # Recurse into properties
        if 'properties' in schema_dict:
            schema_dict['properties'] = {
                k: sanitize_schema_for_google(v) 
                for k, v in schema_dict['properties'].items()
            }
        
        # Recurse into items (for arrays)
        if 'items' in schema_dict:
            schema_dict['items'] = sanitize_schema_for_google(schema_dict['items'])
            
        # Recurse into anyOf / allOf if present
        for key in ['anyOf', 'allOf', 'oneOf']:
            if key in schema_dict:
                schema_dict[key] = [sanitize_schema_for_google(i) for i in schema_dict[key]]

    return schema_dict

# 2. Define the Safe Tool Wrapper
def _build_pydantic_model(schema_dict: Dict[str, Any], tool_name: str) -> type[BaseModel]:
    """Dynamically builds a valid Pydantic model from an MCP JSON schema dict."""
    properties = schema_dict.get("properties", {})
    required = schema_dict.get("required", [])
    
    fields = {}
    for field_name, field_info in properties.items():
        field_type_str = field_info.get("type", "string")
        description = field_info.get("description", "")
        
        # Map JSON schema types to Python types
        if field_type_str == "integer": py_type = int
        elif field_type_str == "number": py_type = float
        elif field_type_str == "boolean": py_type = bool
        elif field_type_str == "array": py_type = list
        elif field_type_str == "object": py_type = dict
        else: py_type = str
        
        # Handle Enums (Force to strings to satisfy Gemini's strict API)
        if "enum" in field_info:
            py_type = str
            enum_vals = [str(v) for v in field_info["enum"]]
            description += f" (Allowed values: {', '.join(enum_vals)})"
        
        # Determine if required and setup default values
        if field_name in required:
            default_val = ...
        else:
            default_val = field_info.get("default", None)
            
        # Create the field annotation (allowing None if it's not required)
        type_annotation = py_type if default_val is ... else py_type | None
        fields[field_name] = (type_annotation, Field(default=default_val, description=description))
        
    # Clean the tool name for the dynamic class creation (no hyphens allowed)
    safe_class_name = tool_name.replace("-", "_").capitalize() + "Schema"
    
    return create_model(safe_class_name, __config__=ConfigDict(extra="allow"), **fields)


# 2. Define the Safe Tool Wrapper
def create_safe_tool(original_tool: BaseTool) -> StructuredTool:
    """
    Creates a StructuredTool that dynamically builds an accurate Pydantic model 
    so Langchain correctly formats the function arguments for Gemini.
    """
    original_schema = original_tool.args_schema
    
    # 1. Ensure we have a dict schema to work with
    if isinstance(original_schema, dict):
        raw_schema = original_schema
    elif isinstance(original_schema, type) and issubclass(original_schema, BaseModel):
        raw_schema = original_schema.model_json_schema()
    else:
        raw_schema = {"properties": {}}

    # 2. Dynamically build a real Pydantic class with actual fields
    SafeSchema = _build_pydantic_model(raw_schema, original_tool.name)

    # 3. Define the execution logic
    async def safe_arun(**kwargs):
        return await original_tool.ainvoke(kwargs)

    def safe_run(**kwargs):
        return asyncio.get_event_loop().run_until_complete(safe_arun(**kwargs))

    return StructuredTool(
        name=original_tool.name,
        description=original_tool.description,
        func=safe_run,
        coroutine=safe_arun,
        args_schema=SafeSchema 
    )

async def main():
    if not GOOGLE_API_KEY:
        print("Error: GOOGLE_API_KEY not found in environment.")
        return

    llm = ChatGoogleGenerativeAI(
        model="gemini-3.1-flash-lite-preview",
        temperature=0.1,
        convert_system_message_to_human=True,
        api_key=GOOGLE_API_KEY
    )

    client = MultiServerMCPClient(
        {
            "openbb": {
                "transport": "http",
                "url": "http://127.0.0.1:6950/mcp",
            }
        }
    )

    print("Connecting to MCP server and loading tools...")
    
    try:
        # 3. Load and Wrap Tools
        raw_tools = await client.get_tools()
        print(f"Loaded {len(raw_tools)} raw tools.")

        # Wrap tools to fix schema issues
        safe_tools = [create_safe_tool(tool) for tool in raw_tools]
        print(f"Wrapped {len(safe_tools)} tools with safe schemas.")

        # 4. Create Agent
        agent = create_agent(llm, safe_tools)

        user_query = "What is the latest closing price for Apple (AAPL)?"
        print(f"Invoking agent with query: {user_query}")

        response = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user_query}]}
        )

        final_message = response["messages"][-1]
        print("\n--- Agent Response ---")
        print(final_message.content)
            
    except* Exception as e:
        print("\n--- AN ERROR OCCURRED ---")
        for exc in e.exceptions:
            print(f"Error Type: {type(exc).__name__}")
            print(f"Error Msg:  {exc}")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            break

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"Top level error: {e}")