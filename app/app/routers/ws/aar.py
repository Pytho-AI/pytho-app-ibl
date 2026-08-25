"""AAR WebSocket endpoint for summary and chat."""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import logging
from handlers import aar
from core.startup import get_executor

router = APIRouter()
logger = logging.getLogger(__name__)


@router.websocket("/aar")
async def websocket_summarize(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection established")
    
    try:
        while True:
            # Receive data from client
            data = await websocket.receive_json()
            action = data.get("action")
            isCloud = data.get("isCloud")
            
            if not action:
                await websocket.send_json({
                    "status": "error",
                    "error": "No action specified"
                })
                continue
            
            logger.info(f"Received action: {action}")
            
            if action == "summary":
                transcript_entries = data.get("transcript", [])
                
                if not transcript_entries:
                    await websocket.send_json({
                        "action": "summary",
                        "status": "error",
                        "error": "No transcript entries provided"
                    })
                    continue
                
                logger.info(f"Processing summary for {len(transcript_entries)} entries")
                
                # Send processing acknowledgment
                await websocket.send_json({
                    "action": "summary",
                    "status": "processing",
                    "message": "Generating summary..."
                })
                
                summary_dict = aar.generate_summary_sync(transcript_entries, isCloud)
                
                logger.info(f"Summary generated: {summary_dict.get('status')}")
                await websocket.send_json(summary_dict)
                
            elif action == "chat":
                question = data.get("question")
                transcript_entries = data.get("transcript", [])
                
                if not question:
                    await websocket.send_json({
                        "action": "chat",
                        "status": "error",
                        "error": "No question provided"
                    })
                    continue
                
                if not transcript_entries:
                    await websocket.send_json({
                        "action": "chat",
                        "status": "error",
                        "error": "No transcript available for context"
                    })
                    continue
                
                logger.info(f"Processing chat question: {question[:50]}...")
                
                # Send processing acknowledgment
                await websocket.send_json({
                    "action": "chat",
                    "status": "processing",
                    "message": "Generating response..."
                })
                
                # Run blocking operation in thread pool
                executor = get_executor()
                if executor is None:
                    await websocket.send_json({
                        "action": "chat",
                        "status": "error",
                        "error": "Thread pool executor not available"
                    })
                    continue
                
                loop = asyncio.get_event_loop()
                chat_dict = await loop.run_in_executor(
                    executor,
                    aar.generate_chat_response_sync,
                    question,
                    transcript_entries,
                    isCloud
                )
                
                logger.info(f"Chat response generated: {chat_dict.get('status')}")
                await websocket.send_json(chat_dict)
            
            elif action == "analyze":
                # Multi-AAR analysis action
                question = data.get("question")
                content = data.get("content", "")
                selected_count = data.get("selected_count", 1)
                
                if not question:
                    await websocket.send_json({
                        "action": "analyze",
                        "status": "error",
                        "error": "No question provided"
                    })
                    continue
                
                if not content:
                    await websocket.send_json({
                        "action": "analyze",
                        "status": "error",
                        "error": "No AAR content provided"
                    })
                    continue
                
                logger.info(f"Processing analysis question: {question[:50]}... ({selected_count} AARs)")
                
                # Send processing acknowledgment
                await websocket.send_json({
                    "action": "analyze",
                    "status": "processing",
                    "message": f"Analyzing {selected_count} AAR input(s)..."
                })
                
                # Run blocking operation in thread pool
                executor = get_executor()
                if executor is None:
                    await websocket.send_json({
                        "action": "analyze",
                        "status": "error",
                        "error": "Thread pool executor not available"
                    })
                    continue
                
                loop = asyncio.get_event_loop()
                analysis_dict = await loop.run_in_executor(
                    executor,
                    aar.generate_analysis_response_sync,
                    question,
                    content,
                    selected_count,
                    isCloud
                )
                
                logger.info(f"Analysis response generated: {analysis_dict.get('status')}")
                await websocket.send_json(analysis_dict)
                
            else:
                await websocket.send_json({
                    "status": "error",
                    "error": f"Unknown action: {action}"
                })
                
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected normally")
    except Exception as e:
        logger.error(f"WebSocket error: {str(e)}", exc_info=True)
        try:
            await websocket.send_json({
                "status": "error",
                "error": str(e)
            })
        except Exception as send_error:
            logger.error(f"Failed to send error message: {str(send_error)}")
