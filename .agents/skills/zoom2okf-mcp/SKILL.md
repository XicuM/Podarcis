---
name: zoom2okf-mcp
description: "Instructions for agents (like @synthesizer) on how to use the zoom2okf-mcp server to transcribe and synthesize video meetings into the OKF v0.2 knowledge base."
disable-model-invocation: true
user-invocable: false
disabled: true
---

# Zoom2OKF MCP Server Skill

## Overview
The **zoom2okf-mcp** server is a specialized tool that performs heavy machine-learning pipelines on Zoom recordings and video files. It runs completely isolated from the core Podarcis framework to avoid dependency bloat.

It handles:
1. Audio Extraction & Transcription (WhisperX)
2. Speaker Diarization (Pyannote)
3. Slide Text Extraction (EasyOCR via ffmpeg keyframing)
4. LLM Chunked Synthesis (Local LLM via OpenAI API)
5. Automatic OKF v0.2 YAML routing into the `wiki/` directory.

## Capabilities Exposed to Agents
When this MCP server is enabled in the Podarcis environment (e.g. via `.mcp.json` or `.podarcis/config.yaml`), it exposes the following tool to the agent network:

- `process_video_to_wiki(video_path: str, time_limit: str = None, keep_transcripts: bool = False, llm_context: str = None, wiki_tree: str = None, extra_prompt: str = None)`
  - **video_path**: The absolute path to the `.mp4` file.
  - **time_limit**: Optional slice for debugging (e.g., `00:01:00-00:05:00`).
  - **keep_transcripts**: If True, saves a `raw_transcription_log.md` backup.
  - **llm_context**: Optional raw text containing prior context or existing wiki notes to inject into the LLM prompt.
  - **wiki_tree**: Optional raw text containing the directory structure of the existing wiki to help the LLM avoid category replication.
  - **extra_prompt**: Optional string containing extra specific instructions for the LLM (e.g., "Extract action items and format them as Markdown checkboxes").

## Agent Instructions for Using Zoom2OKF MCP
If a user asks you (the `@synthesizer` or any other agent) to analyze, summarize, or extract knowledge from a video file:
1. **DO NOT attempt to read or summarize the video file yourself** (you cannot read binary video).
2. **Call the `process_video_to_wiki` tool** provided by the `zoom2okf-mcp` server.
3. Pass the absolute `video_path` to the tool.
4. The tool will synchronously process the video and return the final synthesized OKF v0.2 Markdown string (while simultaneously saving it to the correct `wiki/` subdirectory).
5. If the user asks for the raw transcript to be saved, ensure you pass `keep_transcripts=True`.

## Troubleshooting
If the tool call fails with a CUDA or HuggingFace error:
- Ensure the server environment variables (`HF_TOKEN`, `LLM_API_KEY`) are correctly set in the MCP configuration block.
- Instruct the user to ensure the `pyannote/speaker-diarization-community-1` EULA is accepted on HuggingFace.
