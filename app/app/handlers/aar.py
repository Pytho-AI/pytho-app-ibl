from venv import logger
from utils import llm
import requests
from typing import List, Dict
import re


def format_transcript(transcript_entries: List[Dict]) -> str:
    """
    Format transcript entries into a readable string

    Args:
        transcript_entries: List of transcript entry dicts with 'timestamp', 'speaker', 'text'

    Returns:
        Formatted transcript string
    """
    return "\n\n".join([
        f"[{entry['timestamp']}] {entry['speaker']}: {entry['text']}"
        for entry in transcript_entries
    ])


def create_summary_messages(transcript_text: str) -> List[Dict]:
    prompt = f"""You are a professional meeting summarizer. Below is a transcript of a meeting.

Transcript:
{transcript_text}

Please provide a comprehensive summary that includes:
1. Key Discussion Points - Main topics discussed
2. Action Items - Any tasks or decisions made
3. Important Quotes - Significant statements

Format your summary clearly with headers and bullet points in markdown."""

    return [{'role': 'user', 'content': prompt}]


def markdown_to_text(markdown: str) -> str:
    text = markdown
    text = re.sub(r'```[\s\S]*?```', '', text)          # code blocks
    text = re.sub(r'`([^`]+)`', r'\1', text)            # inline code
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)         # images
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)     # links
    text = re.sub(r'(\*\*|__)(.*?)\1', r'\2', text)     # bold
    text = re.sub(r'(\*|_)(.*?)\1', r'\2', text)        # italics
    text = re.sub(r'^#+\s*(.*)', r'\1', text, flags=re.MULTILINE)  # headings
    text = re.sub(r'^\s*[-*+]\s+', '• ', text, flags=re.MULTILINE) # lists
    text = re.sub(r'^>\s?', '', text, flags=re.MULTILINE)           # blockquotes
    text = re.sub(r'^(-{3,}|_{3,}|\*{3,})$', '', text, flags=re.MULTILINE) # hr
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def generate_summary_sync(transcript_entries: List[Dict]) -> Dict:
    """
    Generate summary synchronously (non-streaming)

    Args:
        transcript_entries: List of transcript entry dicts

    Returns:
        Dict with 'status', 'summary' or 'error' keys
    """
    if not transcript_entries:
        return {
            "status": "error",
            "error": "No transcript entries provided"
        }

    # Format transcript
    full_transcript = format_transcript(transcript_entries)

    # Create messages
    messages = create_summary_messages(full_transcript)

    try:
        summary = llm.invoke_vllm_text(messages, temperature=0.7, timeout=300)

        print(f"Summary complete: {len(summary)} characters")

        return {
            "action": "summary",
            "status": "success",
            "summary": markdown_to_text(summary)
        }

    except requests.exceptions.RequestException as e:
        print(f"Request error: {str(e)}")
        return {
            "status": "error",
            "error": f"Failed to connect to summarization service: {str(e)}"
        }
    except Exception as e:
        print(f"✗ Unexpected error: {str(e)}")
        return {
            "status": "error",
            "error": f"Summarization error: {str(e)}"
        }



def create_chat_messages(question: str, transcript_entries: List[Dict]) -> List[Dict]:
    # Format the transcript
    transcript_text = "\n\n".join([
        f"[{entry['timestamp']}] {entry['speaker']}: {entry['text']}"
        for entry in transcript_entries
    ])

    prompt = f"""You are an AI assistant helping analyze a meeting transcript. Below is the transcript of a meeting, followed by a question.

Transcript:
{transcript_text}

Question: {question}

Please provide a helpful and accurate answer based on the transcript above. If the answer cannot be found in the transcript, politely indicate that."""

    return [{'role': 'user', 'content': prompt}]


def generate_chat_response_sync(question: str, transcript_entries: List[Dict]) -> Dict:
    """
    Generate chat response synchronously

    Args:
        question: User's question
        transcript_entries: List of transcript entry dicts for context

    Returns:
        Dict with 'status', 'response' or 'error' keys
    """
    if not transcript_entries:
        return {
            "status": "error",
            "error": "No transcript available for context"
        }

    if not question or not question.strip():
        return {
            "status": "error",
            "error": "No question provided"
        }

    # Create messages with transcript context
    messages = create_chat_messages(question, transcript_entries)

    try:
        chat_response = llm.invoke_vllm_text(messages, temperature=0.7, timeout=300)

        if not chat_response:
            logger.warning("Received empty response from chat API")
            return {
                "status": "error",
                "error": "Received empty response from AI"
            }

        logger.info(f"Chat response complete: {len(chat_response)} characters")

        return {
            "action": "chat",
            "status": "success",
            "response": chat_response
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {str(e)}")
        return {
            "status": "error",
            "error": f"Failed to connect to chat service: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "error": f"Chat error: {str(e)}"
        }


def create_analyze_messages(question: str, content: str) -> List[Dict]:
    prompt = f"""You are an AI assistant helping analyze lesson learned inputs from an event. Below is combined content from selected inputs (text observations and/or transcripts), followed by a question.

Content:
{content}

Question: {question}

Please provide a helpful and accurate answer based on the content above. If the answer cannot be found in the content, politely indicate that."""

    return [{'role': 'user', 'content': prompt}]


def generate_analyze_response_sync(question: str, content: str) -> Dict:
    """
    Generate analysis response for combined lesson-learned content (local chat API).

    Args:
        question: User's question
        content: Combined text from selected AARs

    Returns:
        Dict with 'status', 'response' or 'error' keys
    """
    if not content or not content.strip():
        return {
            "status": "error",
            "error": "No content available to analyze"
        }
    if not question or not question.strip():
        return {
            "status": "error",
            "error": "No question provided"
        }
    messages = create_analyze_messages(question, content)
    try:
        chat_response = llm.invoke_vllm_text(messages, temperature=0.7, timeout=300)
        if not chat_response:
            return {"status": "error", "error": "Received empty response from AI"}
        return {"status": "success", "response": chat_response}
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {str(e)}")
        return {"status": "error", "error": f"Failed to connect to chat service: {str(e)}"}
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        return {"status": "error", "error": str(e)}


from utils.llm import fetch_llm_response

USER_START = "<start_of_turn>user"
USER_END = "<end_of_turn>"
MODEL_START = "<start_of_turn>model"
MODEL_END = "<end_of_turn>"


def create_analysis_prompt(question: str, combined_content: str, selected_count: int) -> str:
    """
    Create a prompt for multi-AAR analysis
    
    Args:
        question: User's question
        combined_content: Combined content from multiple AARs
        selected_count: Number of AARs selected
    
    Returns:
        Complete prompt string
    """
    prompt = f"""You are an expert After Action Review (AAR) analyst. You are analyzing {selected_count} AAR input(s) which may include audio transcripts and text observations from military exercises or similar events.

Your task is to provide insightful analysis based on the following content:

{combined_content}

Guidelines:
- Be thorough but concise
- Look for patterns across multiple inputs if available
- Highlight key findings, decisions, and lessons learned
- If information is not available to answer the question, say so clearly
- Format your response clearly with bullet points or numbered lists where appropriate
- Each input header includes the contributor's name and role in brackets (e.g., [White Cell] or [Participant])
- When the user asks about specific roles (like "white cell insights" or "participant feedback"), filter and analyze only the inputs from that role

Question: {question}

Please provide a comprehensive answer based on the AAR content above."""

    complete_prompt = f"""{USER_START}
{prompt}{USER_END}
{MODEL_START}
"""
    
    return complete_prompt


# ---- Multi-AAR analysis (ported from main during the edge+cloud
# unification, 2026-07-07; consumed by routers/api/chat.py). ibl's
# older generate_analyze_response_sync (single combined content)
# stays for the edge chat path.
def generate_analysis_response_sync(question: str, combined_content: str, selected_count: int, isCloud: bool) -> Dict:
    """
    Generate analysis response for multiple AARs synchronously
    
    Args:
        question: User's question
        combined_content: Combined content from multiple AARs
        selected_count: Number of AARs selected
        isCloud: If True, use AWS Bedrock; if False, use Ollama chat API
    
    Returns:
        Dict with 'status', 'response' or 'error' keys
    """
    if not combined_content or not combined_content.strip():
        return {
            "action": "analyze",
            "status": "error",
            "error": "No content available in the selected AARs"
        }
    
    if not question or not question.strip():
        return {
            "action": "analyze",
            "status": "error",
            "error": "No question provided"
        }
    
    # Create prompt with combined AAR content
    complete_prompt = create_analysis_prompt(question, combined_content, selected_count)
    
    try:
        if isCloud:
            # Use AWS Bedrock via fetch_llm_response
            prompt_text = complete_prompt
            prompt_text = prompt_text.replace(USER_START, "").replace(USER_END, "").replace(MODEL_START, "").strip()
            
            analysis_response = fetch_llm_response(prompt_text)
            
            # Check if it's an error message
            if analysis_response.startswith("[ERROR]"):
                return {
                    "action": "analyze",
                    "status": "error",
                    "error": analysis_response
                }
            
            if not analysis_response:
                logger.warning("Received empty response from analysis API")
                return {
                    "action": "analyze",
                    "status": "error",
                    "error": "Received empty response from AI"
                }
            
            logger.info(f"Analysis response complete: {len(analysis_response)} characters")
            
            return {
                "action": "analyze",
                "status": "success",
                "response": markdown_to_text(analysis_response)
            }
        else:
            # Use Ollama chat API
            request_body = {
                'model': config.Config.Q_AND_A_MODEL(),
                'prompt': complete_prompt,
                'stream': False,
                'raw': True,
                'temperature': 0.7,
            }
            
            response = requests.post(config.Config.CHAT_API(), json=request_body, timeout=300)
            response.raise_for_status()
            
            data = response.json()
            analysis_response = data.get('response', '').strip()
            
            if not analysis_response:
                logger.warning("Received empty response from analysis API")
                return {
                    "action": "analyze",
                    "status": "error",
                    "error": "Received empty response from AI"
                }
            
            logger.info(f"Analysis response complete: {len(analysis_response)} characters")
            
            return {
                "action": "analyze",
                "status": "success",
                "response": markdown_to_text(analysis_response)
            }
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error: {str(e)}")
        return {
            "action": "analyze",
            "status": "error",
            "error": f"Failed to connect to analysis service: {str(e)}"
        }
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        return {
            "action": "analyze",
            "status": "error",
            "error": f"Analysis error: {str(e)}"
        }
