"""Read-only MCP server, stdio only; strips tunnel credentials before SDK import."""
import os
for _key in list(os.environ):
    if any(word in _key.upper() for word in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
        os.environ.pop(_key, None)

import argparse
import asyncio
import json
from pathlib import Path
import jsonschema
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from reader import Reader, ReadError
from workflow import VERSION, WORKFLOW


def schema(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


ACCESS = {'access_id': {'type': 'string', 'description': 'Current project access_id from project_info'}}
PATH = {**ACCESS, 'path': {'type': 'string', 'description': 'Project-relative file path'}}
DIRECTORY = {'type': 'string', 'description': 'Project-relative subdirectory; default dot means project root'}
SPECS = [
    ('project_info', 'START HERE for every project task, including requests without filenames. Returns current project, access_id, default autonomous discovery workflow, and automatically reads root AGENTS.md and PROJECT_STATUS.md when present. Continue truncated documents with read_markdown, then browse/search relevant sources yourself before asking for filenames.', schema({}, [])),
    ('list_files', 'List Markdown and image paths within the selected project, including subfolders.',
     schema({**ACCESS, 'directory': DIRECTORY, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 10000}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200}}, ['access_id'])),
    ('search_markdown', 'Search literal words in Markdown contents and Markdown/image filenames. Returns source paths, line numbers and snippets. Narrow directory if scan is truncated.',
     schema({**ACCESS, 'query': {'type': 'string', 'minLength': 1, 'maxLength': 256},
             'directory': DIRECTORY, 'offset': {'type': 'integer', 'minimum': 0, 'maximum': 10000},
             'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}, 'case_sensitive': {'type': 'boolean'}}, ['access_id', 'query'])),
    ('read_markdown', 'Read UTF-8 Markdown by line range. Follow image_paths with read_image to actually see figures.',
     schema({**PATH, 'start_line': {'type': 'integer', 'minimum': 1}, 'max_lines': {'type': 'integer', 'minimum': 1, 'maximum': 500}}, ['access_id', 'path'])),
    ('read_image', 'Return actual image pixels for YOUR visual analysis. Defaults to overview. Use page (zero-based) for TIFF; for small labels use crop=[x,y,width,height] in oriented original pixel coordinates, not preview coordinates, and max_side up to 4096. Returned metadata states any intensity conversion.',
     schema({**PATH, 'page': {'type': 'integer', 'minimum': 0},
             'crop': {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}, 'minItems': 4, 'maxItems': 4},
             'max_side': {'type': 'integer', 'minimum': 256, 'maximum': 4096}}, ['access_id', 'path'])),
]


def make_server(reader):
    tools = [types.Tool(name=name, description=desc, inputSchema=s,
                       annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False))
             for name, desc, s in SPECS]

    async def list_tools(context, params):
        return types.ListToolsResult(tools=tools)

    async def call_tool(context, params):
        name, args = params.name, params.arguments or {}
        definition = next((tool for tool in tools if tool.name == name), None)
        try:
            if definition is None:
                raise ReadError('Unknown tool; only read-only tools exist')
            jsonschema.validate(args, definition.input_schema)
            if name == 'project_info':
                result = reader.info()
            else:
                result = getattr(reader, name)(**args)
            if name == 'read_image':
                meta, data, mime = result
                content = [types.TextContent(type='text', text=json.dumps(meta, ensure_ascii=False)),
                           types.ImageContent(type='image', data=data, mimeType=mime)]
            else:
                content = [types.TextContent(type='text', text=json.dumps(result, ensure_ascii=False))]
            reader.authorize(reader.access_id)
            reader.record(name, args.get('path'), 'ok')
            return types.CallToolResult(content=content)
        except (ReadError, jsonschema.ValidationError, OSError) as e:
            reader.record(name if definition else 'unknown', None, 'denied')
            message = str(e) if isinstance(e, ReadError) else 'Invalid arguments or file unavailable'
            return types.CallToolResult(isError=True, content=[types.TextContent(type='text', text=message)])

    return Server('project-reader', version=VERSION,
                  instructions=WORKFLOW,
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.session.read_text(encoding='utf-8'))
    reader = Reader(Path(config['root']), config['access_id'], args.session, args.session.parent / 'audit.jsonl')
    server = make_server(reader)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(run())
