import logging
import json
import uuid
import requests
import os
import re
import slack
import copy
import mistune
import time
import random
import math
import pypandoc
from utils import database, search, generate_audio, config, events, llm, parser
from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader, TextLoader
from handlers import image
from bs4 import BeautifulSoup


slack_client = slack.WebClient(token=os.getenv('SLACK_TOKEN'))


FILE_HOST_API = f'/files/'
MAX_CONTEXT_LENGTH = 100000  # Adjust based on your needs
MAX_QUERY_REFORMULATION_TRY = 2

DOCTRINES_PRODUCT_NAME = "Doctrines"

source_citation_instruction = """
Here are some instructions for handling context:
- If a sentence in your answer is related to a context, put it's id after that sentence.
- Ids must be properly enclosed within <id>...</id> tags, exactly how I have given.
"""

related_questions_instruction = """
After your answer, generate three follow-up questions that the user might want to ask next.
Each question must be enclosed in separate <question> tags. For example:
<question>What are the key factors involved?</question>
<question>How does this compare to alternatives?</question>
<question>What are the next steps?</question>
"""
  

prompt_templates = {
    'short': {
      'max_new_token': 4096,
      'header': """Here is a set of contexts for the question I am going to ask next. Each context is enclosed in a <context> tag""",
      'footer': """
Here are the requirements:
  1. Try to formulate the answer using the set of context I have given.
  2. Use your own knowledge to improve the answer.
  3. Answer the question directly, with no commentary.


Now, answer the following question: """,
      'max_chat_session': 3,
    },
    'medium': {
      'max_new_token': 4096,
      'header': """Here is a set of contexts for the answer you are going to write next. Each context is enclosed in a <context> tag""",
      'footer': """
Here are the requirements:
  1. Try to formulate the answer using the set of <context> I have given.
  2. Use your own knowledge to improve the answer.
  3. Make your answer longer than usual.
  4. Answer the question directly, with no commentary.
  
Now, answer the following question: """,
      'max_chat_session': 3,
    },
    'long': {
      'max_new_token': 6096,
      'header': """Here is a set of contexts for the long answer you are going to write next. Each context is enclosed in a <context> tag""",
      'footer': """
Here are the requirements:
  1. Try to formulate the answer using the set of <context> I have given.
  2. Use your own knowledge to improve the answer.
  3. Make your answer as long as possible. The longer the better.
  4. Answer the question directly, with no commentary.

Now, answer the following question: """,
      'max_chat_session': 2,
    },
}

creativity_instructions = {
    'regular': "",
    'creative': "\n  - You may expand on the provided context with reasonable inferences and new ideas. Keep the core facts accurate but feel free to be creative in how you present and build upon them.\n",
    'very creative': "\n  - Use the provided context as inspiration. Be imaginative — generate new ideas, add narrative flair, speculate, and elaborate well beyond what is strictly in the sources. Creativity is encouraged.\n",
}

def get_creativity_instruction(data):
    """Extract creativity instruction from chatSettings."""
    level = data.get("chatSettings", {}).get("creativity", "Regular").lower()
    return creativity_instructions.get(level, "")


def html_to_docx(html_path, docx_path):
    pypandoc.convert_file(html_path, 'docx', outputfile=docx_path, format='html')

def extract_answer(text, product_type, audio_settings, user_id, project_id):
    if 'productName' in product_type and  product_type['productName'] == 'Audio':
        transcript_pattern = r'[\s|\n|\r]*<transcript>((.|\n)*?)</transcript>[\s|\n|\r]*'
        matches = re.findall(transcript_pattern, text)
        # no transcript was generated
        if len(matches) == 0:
            return text

        return generate_audio.synthesize_speech(
            matches[0][0],
            audio_settings['voice']['code']
        )
    _is_doctrines = isinstance(product_type, dict) and product_type.get('productName') == DOCTRINES_PRODUCT_NAME
    if product_type == '' or _is_doctrines:
        text = mistune.markdown(text)
        text = re.sub(r'>\n<', '><', text)  # Remove newlines between HTML tags
        text = text.replace("&amp;", "&")   # Add back special characters
        text = text.replace("&gt;", ">")
        text = text.replace("&lt;", "<")
        text = text.replace("&quot;", "\"")
        text = text.replace("&#39;", "'")
        text = text.replace("Followup Questions:", "")

        return text
    
    text = parser.keep_only_html(text)
    html_pattern = r'[\s|\n|\r]*<[\s|\n|\r]*body[\s|\n|\r]*>((.|\n)*?)<[\s|\n|\r]*/body[\s|\n|\r]*>[\s|\n|\r]*'
    html_matches = re.findall(html_pattern, text)
    if html_matches:
        content = html_matches[0][0]
    else:
        content = text
    text = f"""<!DOCTYPE html>
<html>
<head>
<style>
table {{
    width: 100%;
    border-collapse: collapse;
}}
th, td {{
    text-align: center;
    padding: 8px;
    border: 1px solid black;
}}
</style>
</head>
<body>
{content}
</body>
</html>
""" # ok to throw exceptions

    file_key = f"{int(time.time())}-{random.randint(1, 1000)}"
    html_path = os.path.join(config.Config.UPLOAD_DIR(), f"{file_key}.html")
    html_url = FILE_HOST_API + f"{file_key}.html"

    with open(html_path, "w", encoding="utf-8") as file:
        file.write(text)

    docx_url = html_url
    try:
        from utils.docx_converter import html_to_docx
        docx_path = os.path.join(config.Config.UPLOAD_DIR(), f"{file_key}.docx")
        html_to_docx(text, docx_path)
        docx_url = FILE_HOST_API + f"{file_key}.docx"
    except Exception as e:
        logging.error(f'DOCX conversion failed, falling back to HTML: {e}')

    events.log_event(user_id, "event:" + project_id, f"Created a {product_type['productName']}")
    return {
        "content": f'<a href="{docx_url}" style="color:#90caf9;" target="_blank">Here</a> is the {product_type["productName"]} you asked for.',
        "htmlLink": html_url,
        "docxLink": docx_url,
        "productName": product_type["productName"],
        "isCustomProduct": True,
    }
  

    # start_index = text.rfind("[/INST]") + len("[/INST]")
    # end_index = text.rfind("</s>")
    # if start_index == -1:
    #     raise Exception("No start index")
    # return text[start_index:end_index].strip()    


def save(data):
    db = database.SessionLocal()
    project_id = 'default-' + data['userId'] if data['projectId'] == 'default' else data['projectId']
    session_id = data["sessionId"]
    timestamp = data["timestamp"]

    try:
        logging.info(f"Saving session: {data['sessionId']} timestamp: {data['timestamp']}")
        print("attempting db session save", "\n", data)
        
        session_detail_data = {
            "userId": data['userId'],
            "projectId": project_id,
            "sessionId": session_id,
            "timestamp": timestamp,
            "title": data['title'],
            "question": data['question'],
            "productType": data['question']['productType'], 
            "answer": data['answer'],
            "sessionPrompt": data["session"][-1][0],
            "sessionAnswer": data["session"][-1][1],
            "sourcesMap": data["session"][-1][2],
            "htmlLink": data.get("htmlLink"),
            "docxLink": data.get("docxLink"),
            "isCustomProduct": data.get("isCustomProduct"),
            "productName": data.get("productName"),
        }
        
        print("reached db session detail creation")
        session_detail = database.SessionDetail(
            session_id=session_id,
            timestamp=timestamp,
            project_id=project_id,
            data=database.null_safe_json_dumps(session_detail_data)
        )
        db.add(session_detail)
        print("reached db session detail add")
        if len(data["session"]) == 1:
            print("attempting db session data add")
            session_data = {
                "userId": data['userId'],
                "projectId": project_id,
                "sessionId": session_id,
                "timestamp": timestamp,
                "title": data['title'],
                "sessionDocs": data.get('sessionDocs'),
                # dataSources: data.dataSources,
                "docsSummary": data.get('docsSummary'),
                # "isShared": data['isShared'], 
            }
            print("reached db session data add")
            session = database.Session(
                session_id=session_id,
                timestamp=timestamp,
                project_id=project_id,
                data=database.null_safe_json_dumps(session_data)
            )
            db.add(session)
            print("completed db session data add")
        db.commit()
        print("completed db commit data")
    except Exception as e:
        logging.error(f'Failed to save session\n{e}')
    finally:
        db.close()

def get_context_from_docs(docs, docs_summary=None):
    if docs_summary:
        return summarized_docs_prompt(docs_summary, docs)

    context_log = ""
    sources_map = {}
    context_temp = ""
    docs_with_pages = []

    for d, doc in enumerate(docs):
        doc_key = doc["key"]
        file_path = os.path.join(config.Config.UPLOAD_DIR(), doc_key)
        
        if doc_key.lower().endswith(".pdf"):
            loader = PyPDFLoader(file_path)
        elif doc_key.lower().endswith(".docx"):
            loader = Docx2txtLoader(file_path)
        elif doc_key.lower().endswith(".txt"):
            loader = TextLoader(file_path)
        
        pages = loader.load_and_split()
        context_log += f"Using {len(pages)} pages from {doc_key}\n"
        docs_with_pages.append({"doc": doc, "pages": pages})
        
        context_temp += f"\nFollowing <context>s are the pages from {doc['name']}:\n"
        for i, page in enumerate(pages):
            id = f"doc.{d}#page={i+1}"
            sources_map[id] = {
                "id": id,
                "link": doc['url'] + f"#page={i+1}",
                "title": doc["name"],
                "linkLabel": f"Page {i+1}"
            }
            
            page_context = f"""

<context>
  {page.page_content.strip()}
  <id>{id}</id>
</context>"""
            context_temp += page_context

        context_temp += f"\nEnd of all the pages from {doc['name']}\n"
    
    if len(context_temp) > MAX_CONTEXT_LENGTH:
        docs_summary = summarize_docs(docs_with_pages)
        return summarized_docs_prompt(docs_summary, docs)
    

    return {
        "sources_map": sources_map, 
        "context": context_temp, 
        "context_log": context_log
    }

def get_modified_question(question, result):
    prompt = f"""You are a "Query Reformulator" embedded in a Retrieval-Augmented Generation (RAG) pipeline.
Your job, given the user's original question and the retrieved context, create a better question for the retriever.

Original question: {question}
Retrieved context: {result.get('context', '')}

Here are the requirements:
  1. Generate a question that specifically requests the critical information still missing from the retrieved context.
  2. Phrase it at a higher, summary level--for example: "Provide the fact sheets for the C-17."
  3. Enclose the entire question within <question> ... </question> tags."""

    modified_query = llm.invoke_vllm_text(
        [{'role': 'user', 'content': prompt}],
        temperature=0.9,
    )
    print(f"Modified query: {modified_query}")

    match = re.search(r'(?i)<question>(.*?)</question>', modified_query, re.DOTALL)
    return match.group(1).strip() if match else None

def get_context_with_retry(question, num_retry=1, index_name=search.INDEX_NAME):
    result = {}
    current_try = 0
    modified_question = question

    while True:
        result = get_context(modified_question, result, index_name=index_name)
        current_try += 1

        if current_try >= num_retry:
            break
        modified_question = get_modified_question(question, result)
        if not modified_question:
            break

    return result

def summarized_docs_prompt(docs_summary, docs):
    sources_map = {}
    context_log = f"\n\nUsing summary\n\nOriginal: {json.dumps(docs, indent=2)}\n\n"
    context = docs_summary
    
    return {
        "sources_map": sources_map, 
        "context": context, 
        "context_log": context_log, 
        "docs_summary": docs_summary
    }


def summarize_docs(docs):
    context = ""
    
    for doc in docs:
        doc_content = ""
        length = 0
        try:
            for page in doc["pages"]:
                doc_content += f"<page>\n{page.page_content.strip()}\n</page>\n"
                length += len(doc_content)
                if length > MAX_CONTEXT_LENGTH:
                    print(f"Cutting short {doc['doc']['name']}")
                    break
            
            prompt = (f"Here are some pages from a file titled {doc['doc']['name']}. Each page is enclosed inside <page> tag.\n"
                      f"{doc_content}\n"
                      "Here are the requirements:\n"
                      "  1. Create a summary of facts from all the pages.\n"
                      "  2. Pay attention to all the pages.\n"
                      "  3. Do not miss any interesting fact.\n"
                      "  4. Make your summary as detailed as possible.")
            
            text = llm.invoke_vllm_text(
                [{'role': 'user', 'content': prompt}],
                temperature=0.9,
            )

            context += f"\nFollowing <context> is the summary of {doc['doc']['name']}:\n<context>\n{text}\n</context>\n"
        except Exception as e:
            print(f"Exception in processing large doc: {doc['doc']['name']}. {e}")
    
    return context  


def get_context(question, previous_context, context_count=4, index_name=search.INDEX_NAME):
    matched_docs = search.top_k_similar_chunks(question, context_count, index_name=index_name)

    context_log = previous_context.get('context_log', '') + '\nCONTEXT\n\n'
    sources_map = previous_context.get('sources_map', {})
    context = previous_context.get('context', '')
    previous_chunks = {(d["doc_id"], d["chunk_id"]) for d in sources_map.values()}

    for doc in matched_docs:
        id = len(sources_map)
           
        if (doc["doc_id"], doc["chunk_id"]) not in previous_chunks and doc["score"] > 0.70:
            
            context_log += f'doc_id: {doc["doc_id"]}\nchunk_id: {doc["chunk_id"]}\nsimilarity:{doc["score"]}\n{doc["link"]}\n\n'
            content = doc['content'].strip()
            link = doc['link']
            if doc.get('page') and link and '#' not in link:
                link = f"{link}#page={doc['page']}"
            sources_map[str(id)] = {
                'id': id,
                'title': doc['title'],
                'linkLabel': "Page " + str(doc['page']),
                'link': link,
                'doc_id': doc['doc_id'],
                'chunk_id': doc['chunk_id'],
            }
            
            context += f"""

<context>
  {content}
  <id>{id}</id>
</context>"""

        else:
            context_log += f'doc_id: {doc["doc_id"]}\nchunk_id: {doc["chunk_id"]}\nsimilarity:{doc["score"]} <dropped>\n\n'            

    return {
        'sources_map': sources_map,
        'context': context,
        'context_log': context_log
    }

def parse_fb_posts_to_json(xml_text):
    """Parse Facebook post XML and convert to frontend-compatible JSON format."""
    fb_injects = []
    
    # Extract all <post> blocks
    post_pattern = r'<post>(.*?)</post>'
    post_matches = re.findall(post_pattern, xml_text, re.DOTALL)
    
    for post_text in post_matches:
        # Extract name
        name_match = re.search(r'<name>(.*?)</name>', post_text, re.DOTALL)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        
        # Extract gender (for avatar selection if needed)
        gender_match = re.search(r'<gender>(.*?)</gender>', post_text, re.DOTALL)
        gender = gender_match.group(1).strip() if gender_match else "male"
        
        # Extract message
        message_match = re.search(r'<message>(.*?)</message>', post_text, re.DOTALL)
        if not message_match:
            continue
        message = message_match.group(1).strip()
        

        # image_match = re.search(r'<image>(.*?)</image>', post_text, re.DOTALL)
        

        avatar = f"/avatars/{random.randint(1, 15)}.jpg"
        
        # Build Facebook post inject object matching frontend format
        fb_inject = {
            "user": {
                "name": name,
                "avatar": avatar,
            },
            "hour": random.randint(1, 24),
            "text": message,
        }
        
        # Add media if image exists
        # if image_match:
        #     fb_inject["media"] = image_match.group(1).strip()
        
        fb_injects.append(fb_inject)
    
    return fb_injects


def parse_tweets_to_json(xml_text):
    """Parse tweet XML and convert to frontend-compatible JSON format."""
    tweet_injects = []
    
    # Extract all <tweet> blocks
    tweet_pattern = r'<tweet>(.*?)</tweet>'
    tweet_matches = re.findall(tweet_pattern, xml_text, re.DOTALL)
    
    for tweet_text in tweet_matches:
        # Extract name
        name_match = re.search(r'<name>(.*?)</name>', tweet_text, re.DOTALL)
        if not name_match:
            continue
        name = name_match.group(1).strip()
        
        # Extract gender (for avatar selection if needed)
        gender_match = re.search(r'<gender>(.*?)</gender>', tweet_text, re.DOTALL)
        gender = gender_match.group(1).strip() if gender_match else "male"
        
        # Extract message
        message_match = re.search(r'<message>(.*?)</message>', tweet_text, re.DOTALL)
        if not message_match:
            continue
        message = message_match.group(1).strip()
        
        # image_match = re.search(r'<image>(.*?)</image>', tweet_text, re.DOTALL)
        
        # Generate nickname from name
        nickname = name.replace(' ', '_').lower() + str(random.randint(0, 99))
        

        avatar = f"/avatars/{random.randint(1, 15)}.jpg"
        
        # Build tweet inject object matching TweetConfig type
        tweet_inject = {
            "user": {
                "nickname": nickname,
                "name": name,
                "avatar": avatar,
                "verified": random.random() > 0.6,
                "locked": False
            },
            "display": "default",
            "text": message,
            "date": "",  # You can add timestamp logic here if needed
            "app": "",   # You can specify Twitter client app if needed
            "retweets": random.randint(0, 100),
            "quotedTweets": random.randint(0, 10),
            "likes": random.randint(0, 10000)
        }
        
        # Add image if exists
        # if image_match:
        #     tweet_inject["image"] = image_match.group(1).strip()
        
        tweet_injects.append(tweet_inject)
    
    return tweet_injects


def generate_fb_template(question, postCount, creativity_instruction=""):
    print(postCount)
    """Generate Facebook posts using LLM and return processed JSON."""
    prompt = f'''You are going to generate some Facebook <post>.
Here are the requirements:
    1. Wrap each post inside <post> tags.
    2. Inside each post put a <name>, <gender>, <message> tags.
    3. Optionally include <image> tags for posts with images (use image URLs).
    4. Make the names realistic and civilian.
    5. Gender should be either "male" or "female".
    6. Make the <message> detailed, similar to real Facebook posts.
{creativity_instruction}
Now, generate {postCount} posts for the following request: {question}
'''

    response_text = llm.invoke_vllm_text(
        [{'role': 'user', 'content': prompt}],
        temperature=0.9,
    )

    # Process and return structured JSON
    return prompt, parse_fb_posts_to_json(response_text), response_text


def generate_twitter_template(question, postCount, creativity_instruction=""):
    print(postCount)
    """Generate tweets using LLM and return processed JSON."""
    prompt = f'''You are going to generate some <tweet>.
Here are the requirements:
  1. Wrap each tweet inside <tweet> tags.
  2. Inside each tweet put a <name>, <gender>, <message> tags.
  3. Optionally include <image> tags for tweets with images (use image URLs).
  4. Make the names realistic and civilian.
  5. Gender should be either "male" or "female".
  6. Occasionally add hashtags in the message.
{creativity_instruction}
Now, generate {postCount} tweets for the following request: {question}
'''

    response_text = llm.invoke_vllm_text(
        [{'role': 'user', 'content': prompt}],
        temperature=0.9,
    )

    # Process and return structured JSON
    return prompt, parse_tweets_to_json(response_text), response_text

def doc_translate(data):
    """Translate an HTML document to a target language using the LLM."""
    target_language = data.get('targetLanguage', 'English')
    content = data.get('content', '')
    logging.info(f'[doc_translate] Starting translation to {target_language}, content length: {len(content)} chars')

    prompt = f"""Translate the following HTML document to {target_language}.

Rules:
1. Translate ALL visible text content to {target_language}.
2. Preserve ALL HTML tags, attributes, styles, structure, and formatting exactly as they are.
3. Do NOT translate HTML tag names, attribute names, or attribute values (like class names, ids, URLs, etc.).
4. Return ONLY the translated HTML with no explanations, no markdown fences, no extra text.

HTML Document:
{content}
"""

    try:
        logging.info(f'[doc_translate] Calling LLM for {target_language} translation...')
        translated = llm.invoke_vllm_text([
            {"role": "user", "content": prompt}
        ])
        logging.info(f'[doc_translate] LLM returned {len(translated)} chars')

        # Clean up markdown fences if the LLM wraps the response
        if translated.startswith('```'):
            lines = translated.split('\n')
            lines = lines[1:]  # remove opening fence
            if lines and lines[-1].strip() == '```':
                lines = lines[:-1]
            translated = '\n'.join(lines)

        data['type'] = 'doc_translate_result'
        data['status'] = 'success'
        data['content'] = translated
        logging.info(f'[doc_translate] Translation to {target_language} complete')

    except Exception as e:
        logging.error(f'[doc_translate] Error: {e}', exc_info=True)
        data['type'] = 'doc_translate_result'
        data['status'] = 'error'
        data['error'] = str(e)

    return json.dumps(data)


def doc_chat(data):
    prompt = f"""
You are an expert HTML editor specialized in localized document modifications.

You will be provided with:
1. The **full HTML document** for reference.
2. A **specific context (HTML section)** that needs modification.
3. A **user instruction** describing what to change.

Your task:
Modify only the given context according to the instruction. Use the full HTML document only for reference or consistency (e.g., to preserve structure, style, or links), but do NOT rewrite or alter parts outside the context.

Inputs:
---
HTML Document:
{data.get('document', '')}

Context (to modify):
{data.get('context', '')}

User Instruction:
{data.get('question', {}).get('prompt')}

Output Requirements:
1. Return **only** the updated context section in valid HTML.
2. Preserve all existing HTML tags, structure, attributes, links, styles, and formatting unless explicitly instructed to change them.
3. Do not include explanations, reasoning, or additional text -- only the modified HTML snippet.

Now, output the updated context in HTML.
"""

    try:
        text = llm.invoke_vllm_text([
            {"role": "user", "content": prompt}
        ])
        print(text)
        # Gemma-4 leaks internal reasoning tags despite "no reasoning" prompts:
        # <thought>...</thought> blocks and <|channel|>-style chat-template tokens.
        # Strip them before parsing so the HTML extraction below doesn't grab
        # them as content or break on truncated tags.
        text = re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<\|[^|]*\|>", "", text)
        # Strip ```html / ``` fence markers if the model wrapped its response.
        text = re.sub(r"^\s*```(?:html)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        text = text.strip()
        # Parse HTML response. When Gemma honors the prompt's "return only the
        # snippet" instruction (no <html><body> wrapper), soup.body is None —
        # fall back to top-level soup contents in that case.
        soup = BeautifulSoup(text, 'html.parser')
        container = soup.body if soup.body is not None else soup
        extracted = [str(tag) for tag in container.contents if getattr(tag, 'name', None)]

        data['answer'] = ''.join(extracted)
        if len(data['answer']) < 1:
            # Last-resort: if the model returned plain-text or BeautifulSoup
            # couldn't find a tagged child, return the scrubbed text wrapped
            # in <p> so the editor at least shows what the model said.
            if text:
                data['answer'] = f'<p>{text}</p>'
            else:
                data['errorMessage'] = 'Missing HTML'

    except Exception as e:
        print(e)
        data['errorMessage'] = 'Sorry! Please try again later'


def main(data, on_status=None):
    print(data)
    logging.info('main chat')
    session = data.get('session', [])
    question = data['question']['prompt'].replace('\n', ' ')
    user_id = data['userId']
    alert_log = f'{user_id}\nQUESTION: {question}\n' 
    context_log = ''
    sources_map = {}
    context = ''
    general_context = ''
    chat_messages = []
    product_type = data['question']['productType']
    product_name = ''
    if product_type:
        product_name = product_type['productName']
    
    text = 'Something went wrong!'
    
    if product_name == "doc_chat":
        doc_chat(data)
        return json.dumps(data)
        
    if product_name == "Image":
        print("Generating image...")
        logging.info(f'Image generation requested: {question}')
        data['title'] = question[:100]
        # Generate the image with hardcoded settings
        result = image.generate_image(
            prompt=question,
        )
        print(result)

        data["answer"] = result.strip()
        session.append([question, data["answer"], {}])
        data['session'] = session
        data['timestamp'] = math.floor(time.time() * 1000)
        save(data)
        return json.dumps(data)
        
    
    if product_name == "Facebook Post":
        print("here fb")
        post_count = data['question']['productType']["postCount"]
        data['title'] = question
        prompt, answer, response_text = generate_fb_template(question, post_count, get_creativity_instruction(data))
        post_texts = [inject.get("text", "") for inject in answer]
        for i, pt in enumerate(post_texts):
            logging.info("Facebook post %s/%s: generating image for text length %s", i + 1, len(answer), len(pt))
        urls = image.generate_image_urls(
            post_texts,
            width=image.POST_IMAGE_WIDTH,
            height=image.POST_IMAGE_HEIGHT,
        )
        for i, inject in enumerate(answer):
            try:
                if i < len(urls) and urls[i]:
                    inject["media"] = urls[i]
                    logging.info("Facebook post %s: attached media %s", i + 1, urls[i])
                else:
                    logging.warning("Facebook post %s: no image URL returned (SD may be down or prompt failed)", i + 1)
            except Exception as e:
                logging.warning("Image generation skipped for Facebook post %s: %s", i + 1, e, exc_info=True)
        data['answer'] = answer
        session.append([prompt, response_text, {}])
        data['session'] = session
        data['timestamp'] = math.floor(time.time() * 1000)
        save(data)
        return json.dumps(data)
        

    elif product_name == "Tweet":
        print("here tweet")
        post_count = data['question']['productType']["postCount"]
        data['title'] = question
        prompt, answer, response_text = generate_twitter_template(question, post_count, get_creativity_instruction(data))
        data['answer'] = answer
        session.append([prompt, response_text, {}])
        data['session'] = session
        data['timestamp'] = math.floor(time.time() * 1000)
        save(data)
        return json.dumps(data)
    
    else:
        try:
            _is_docgen = (
                data['question']['productType'] not in ('', None)
                and isinstance(data['question']['productType'], dict)
                and data['question']['productType'].get('template_body_path')
                and os.getenv("SKIP_DOCGEN", "").lower() not in ("1", "true", "yes")
            )
            if _is_docgen and on_status:
                on_status("Preparing document generation...")

            _is_doctrines = product_name == DOCTRINES_PRODUCT_NAME
            _retrieval_index = search.DOCTRINE_INDEX_NAME if _is_doctrines else search.INDEX_NAME

            if data['baseline']['docs'] or _is_doctrines:
                # DocGen: skip LLM reformulation, just do direct vector search
                _retries = 1 if _is_docgen else MAX_QUERY_REFORMULATION_TRY
                context_result = get_context_with_retry(question, _retries, index_name=_retrieval_index)
                context += context_result['context']
                sources_map.update(context_result['sources_map'])
                context_log += context_result['context_log']

            if len(data['docs']) > 0:
                context_result = get_context_from_docs(data['docs'], data.get('docsSummary'))
                general_context += context_result['context']
                chat_messages = [
                    {'role': 'user', 'content': f"""Here are some contexts for all the following questions. Each context is enclosed in a <context> tag.

{general_context}

Here are the requirements which applies to all the following questions:
1. Try to formulate the answer using the set of <context> I have given.
2. Use your own knowledge to improve the answer.
3. Answer the question directly, with no commentary."""},
                    {'role': 'assistant', 'content': 'Certainly! I will use the <context> you have given while answering the following questions.'},
                ]
                sources_map.update(context_result['sources_map'])
                context_log += context_result['context_log']
                data['docsSummary'] = context_result.get('docs_summary')
                
            prompt_key = data["chatSettings"]["answerSize"].lower()
            
                    
            include_related = data.get("chatSettings", {}).get("includeRelatedQuestions", False)
            logging.info(f"includeRelatedQuestions setting: {include_related}, type: {type(include_related)}, chatSettings: {data.get('chatSettings')}")

            if data['question']['productType'] == '' or _is_doctrines: # ask anything OR doctrines

                prompt = f"""
{prompt_templates[prompt_key]['header']}
{context}
"""
                if (len(context) + len(general_context) > 0) and data["chatSettings"]["shouldCiteSources"]:
                    prompt += source_citation_instruction

                if include_related:
                    prompt += related_questions_instruction

                creativity_inst = "" if _is_doctrines else get_creativity_instruction(data)
                prompt += f"""
{prompt_templates[prompt_key]['footer']}{creativity_inst}{question}"""
                    
            
            elif data['question']['productType']['productName'] == 'Audio':
                # avoid LLM call
                if data['audioSettings']['transcript']['type'] == 'custom':
                    logging.info("using custom transcript")
                    text = generate_audio.synthesize_speech(
                        data['audioSettings']['transcript']['text'],
                        data['audioSettings']['voice']['code']
                    )
                    session.append([prompt, text, copy.deepcopy(sources_map)])
                    data['title'] = question
                    data['answer'] = text.strip()
                    data['session'] = session
                    data['timestamp'] = math.floor(time.time() * 1000)
                    save(data)
                    return json.dumps(data)
                else:
                    logging.info("generating audio transcript")
                    audio_language = data.get('audioSettings', {}).get('language', {}).get('name', 'English (US)')
                    speaker_gender = data.get('audioSettings', {}).get('voice', {}).get('gender', 'female')

                    prompt = f"""
You are going to generate a transcript for a single-speaker monologue. Here are some contexts for the transcript you are going to generate. Each context is enclosed in a <context> tag.
{context}

Here are the requirements:
1. Do not include any instructions, descriptions, or annotations.
2. Only the spoken words should appear.
3. Wrap the transcript inside <transcript> tags.
4. The entire transcript MUST be written in {audio_language}. Do not use any other language.
5. The speaker is {speaker_gender}. Use appropriate gendered language where applicable.
{get_creativity_instruction(data)}
Now, generate the transcript in {audio_language} for the following request: {question}""" 

            else: # templated product
                skip_docgen = os.getenv("SKIP_DOCGEN", "").lower() in ("1", "true", "yes")
                product_type = data['question']['productType']
                template_path = product_type.get('template_path', '')

                if not skip_docgen:
                    try:
                        from handlers.docgen import generate_document
                        # Resolve the original PDF path (strip .html suffix if it's the converted version)
                        pdf_path = template_path
                        if pdf_path.endswith('.html'):
                            pdf_path = pdf_path.replace('.html', '').replace('_body', '')
                        if not os.path.exists(pdf_path):
                            pdf_path = None

                        def _docgen_notify(event_type, content):
                            if event_type == "status" and on_status:
                                on_status(content)

                        # Read pre-converted HTML from upload time (fast, no re-conversion)
                        template_body_path = product_type.get('template_body_path', '')
                        template_text = ""
                        if template_body_path and os.path.exists(template_body_path):
                            with open(template_body_path, "r", encoding="utf-8") as f:
                                template_text = f.read()

                        result_html = generate_document(
                            task=question,
                            steps=[],
                            template_path=pdf_path,
                            additional_context=template_text or None,
                            model=config.Config.Q_AND_A_MODEL(),
                            on_event=_docgen_notify,
                        )

                        if result_html:
                            # Use extract_answer to save HTML, convert to DOCX, and get links
                            text = extract_answer(f"<body>{result_html}</body>", product_type, data.get('audioSettings'), user_id, data['projectId'])
                            session.append([question, result_html[:500], copy.deepcopy(sources_map)])
                            data['title'] = question
                            data['answer'] = text['content']
                            data['htmlLink'] = text.get('htmlLink')
                            data['docxLink'] = text.get('docxLink')
                            data['isCustomProduct'] = True
                            data['productName'] = text.get('productName')
                            data['session'] = session
                            data['timestamp'] = math.floor(time.time() * 1000)
                            save(data)
                            return json.dumps(data)
                    except Exception as e:
                        logging.error(f"DocGen failed in chat path, falling back: {e}")
                        import traceback
                        traceback.print_exc()

                # Fallback: original template-based generation
                with open(data['question']['productType']['template_body_path'], "r", encoding="utf-8") as file:
                    template = f"{file.read()}"

                prompt = f"""
I am going to give you some context and a sample HTML <body> template for {data['question']['productType']["productName"]}. Each context is enclosed in a <context> tag.
{context}

Here is the sample template inside <body> tags: 
{template}

Here are the requirements:
1. Make your answer as detailed as possible and put it inside <body> tags.
2. Your answer must be valid HTML following the template I have given.
3. Do not add any extra attribute inside <body> tag.
4. Try to formulate the answer using the set of <context> I have given.
5. Use your own knowledge to improve the answer.
{get_creativity_instruction(data)}
Now, create a {data['question']['productType']["productName"]} for the following question: {question}"""
                
            
            combined_source_map = sources_map
            for s in session:
                chat_messages.append({'role': 'user', 'content': s[0]})
                chat_messages.append({'role': 'assistant', 'content': s[1]})
                combined_source_map.update(s[2])

            chat_messages.append({'role': 'user', 'content': prompt})

            context_log += str(chat_messages)

            response = llm.invoke_vllm_text(
                chat_messages,
                temperature=0.5,
            ).replace('Meta AI', 'Pytho AI').replace('OpenAI', 'Pytho AI').replace('LLaMA', 'Pytho')
            context_log += f'\n\nuser: {user_id}\nquestion:\n{question}\nassistant:\n{response.encode().decode("unicode-escape")}'
            text = extract_answer(response, data['question']['productType'], data.get('audioSettings'), user_id, data['projectId'])

            session.append([prompt, response, copy.deepcopy(sources_map)])
            if len(session) >= prompt_templates[prompt_key]['max_chat_session']:
                session.pop(0)

            # Source citations only apply to non-custom-product (string) responses
            if isinstance(text, str):
                if not data["chatSettings"]["shouldCiteSources"]:
                    text = re.sub(r"<id>[^<]+</id>", "", text)
                matches = re.findall("<id>([^<]+)</id>", text)
                currentRefIndex = -1

                context_log += "Sending the following sources to the user\n\n"
                for reference in matches:
                    if reference in combined_source_map:
                        source = combined_source_map[reference]
                        if 'refIndex' not in source:
                            currentRefIndex += 1
                            source['refIndex'] = currentRefIndex
                            combined_source_map[reference] = source        
                        
                sources = list(filter(lambda s: 'refIndex' in s, combined_source_map.values()))
                sources.sort(key=lambda x: x['refIndex'])

                if len(sources) > 0:
                    text += "<b>References</b><br/><ol style=\"font-size:.85rem; \">"
                    
                    for s in sources:
                        context_log += "\n" + s["link"]
                        linkHTML = f'<a href="{s["link"]}" style="color:#90caf9;font-size:.85rem;" target="_blank">[{s["refIndex"] + 1}]</a>'
                        text = text.replace(f'<id>{s["id"]}</id>', linkHTML)
                        text += f'<li>{s["title"]} <a style="color:#90caf9;" href="{s["link"]}" target="_blank">{s["linkLabel"]}</a></li>'
                    
                    text += "</ol>"

        except Exception as e:
            logging.exception(f'user: {user_id}\nquestion:\n{question}\n')
            alert_log += str(e)

        data['title'] = question
        if isinstance(text, dict):
            data['answer'] = text['content'].strip()
            data['htmlLink'] = text.get('htmlLink')
            data['docxLink'] = text.get('docxLink')
            data['productName'] = text.get('productName')
            data['isCustomProduct'] = True
        else:
            data['answer'] = text.strip()
        data['session'] = session
        data['timestamp'] = math.floor(time.time() * 1000)
        alert_log += f'\n\nANSWER: {data["answer"]}'
        logging.info(context_log)
        save(data)
        print("done")
        # try:
        #   slack_client.chat_postMessage(channel='pytho-chats-demo',text=alert_log)
        # except Exception as e:
        #   print()  
        return json.dumps(data)