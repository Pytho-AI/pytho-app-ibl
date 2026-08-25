import logging
import json
import os
import re
import slack
import time
import random
import math
import pypandoc
from bs4 import BeautifulSoup
from utils import database, search, generate_audio, config
from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader, TextLoader
from typing import Dict, List, Any
from utils import llm



slack_client = slack.WebClient(token=os.getenv('SLACK_TOKEN'))


FILE_HOST_API = f'/files/'
MAX_CONTEXT_LENGTH = 100000  # Adjust based on your needs
MAX_QUERY_REFORMULATION_TRY = 2



def html_to_docx(html_path, docx_path):
    pypandoc.convert_file(html_path, 'docx', outputfile=docx_path, format='html')

def extract_answer(text, product_type):
    html_pattern = r'[\s|\n|\r]*<html>((.|\n)*?)</html>[\s|\n|\r]*'
    html_matches = re.findall(html_pattern, text)

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
{html_matches[0][0]}
</body>
</html>
""" # ok to throw exceptions

    file_key = f"{int(time.time())}-{random.randint(1, 1000)}"
    html_path = os.path.join(config.Config.UPLOAD_DIR(), f"{file_key}.html")
    # docx_path = os.path.join(UPLOAD_DIR, f"{file_key}.docx")
    # url = FILE_HOST_API + f"{file_key}.docx"
    url = FILE_HOST_API + f"{file_key}.html"

    with open(html_path, "w", encoding="utf-8") as file:
        file.write(text)
    # html_to_docx(html_path, docx_path)

    return f'<a href="{url}" style="color:#90caf9;" target="_blank">Here</a> is the {product_type["productName"]} you asked for.'
  

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
            "type": data.get("type"),
            "question": data['question'],
            "productType": data['question']['productType'], 
            "answer": data.get('answer'),
            "plan": data.get("plan"),
            "sessionPrompt": data["session"][-1][0],
            "sessionAnswer": data["session"][-1][1],
            "sourcesMap": data["session"][-1][2],  
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
                "type": 'agentic',
                "title": data['title'],
                "plan": data.get("plan"),
                "docs": data['docs'],
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

def get_context_with_retry(question, num_retry=1):
    result = {}
    current_try = 0
    modified_question = question

    while True:
        result = get_context(modified_question, result)
        current_try += 1
        
        if current_try >= num_retry:
            break
        modified_question = get_modified_question(question, result)
        if not modified_question:
            break

    return result 

def process_task(text: str):
    task_pattern = re.compile(r"<task>((.|\n)*?)</task>", re.MULTILINE)
    matches = list(task_pattern.finditer(text))
    
    if not matches:
        return {"text": text, "task": ""}
    
    return {
        "text": task_pattern.sub("", text),
        "task": matches[0].group(1)
    }

def process_plan(text: str):
    plan_pattern = re.compile(r"<plan>((.|\n)*?)</plan>", re.MULTILINE)
    matches = list(plan_pattern.finditer(text))
    plan = ""
    
    if not matches:
        ul_start_index = text.find("<ul>")
        ul_end_index = text.rfind("</ul>")

        # LLM sometimes forgets to put the plan inside <plan>
        if ul_start_index > -1 and ul_end_index > -1:
            plan = text[ul_start_index:ul_end_index] + "</ul>"
            text = text[:ul_start_index]
        else:
            return {"steps": [], "text": text}
    else:
        plan = matches[0].group(1)
        plan_start_index = text.find("<plan>")
        text = text[:plan_start_index]
    
    soup = BeautifulSoup(plan, "html.parser")
    ul = soup.find("ul")
    if not ul:
        steps = []
    else:
        steps = [str(li) for li in ul.find_all("li", recursive=False)]

    return {
        "steps": steps,
        "text": text + "\n"
    }

template_pattern = re.compile(r"<body>((.|\n)*?)<\/body>", re.MULTILINE)
    
def get_template(template_path):
    with open(template_path, "r", encoding="utf-8") as file:
        product_template = file.read()  
    
    template_matches = list(template_pattern.finditer(product_template))
    if len(template_matches) < 1:
        print(f"No matching templates found: {product_template}")
        return ""
    return f"<html>{template_matches[0].group(1)}</html>"

def plan(data: Dict[str, Any], docs_context):
    
    prompt = data["question"]["prompt"]
    
    # Already created a plan
    if data.get("plan"):
        prompt = (
            "Acknowledge Completion: If the user accepts the plan, let them know that we are executing on the plan "
            "and add |DONE| at the end of your response.\n\n"
            f"Now, answer this question: {prompt}\n"
        )

    general_context = """You are a helpful AI agent specializing in generating plans for defense related use cases. 
Your role is to create a structured plan that another AI agent will execute. 
The plan should be formatted as an unordered HTML list of steps.

Example Task & Plan:
Task: "Create an HVT List for key targets in the 71st ACG."

<plan>
  <ul>
    <li>Find HVT List format</li>
    <li>Create a real-time threat Order of Battle to assess the current composition, disposition, and strength</li>
    <li>Conduct network analysis</li>
    <li>Identify targets of interest</li>
    <li>Assess target value and impact</li>
    <li>Generate a prioritized HVT List</li>
  </ul>
</plan>

Task: "Create a SITEMP for 71st ACG"

<plan>
  <ul>
    <li>Define the Operational Environment (OE)
      <ul>
        <li>Identify AO (Area of Operations) and AI (Area of Interest)</li>
        <li>Analyze terrain and weather using OCOKA:
          <ul>
            <li>Observation and fields of fire</li>
            <li>Cover and concealment</li>
            <li>Obstacles</li>
            <li>Key terrain</li>
            <li>Avenues of approach</li>
          </ul>
        </li>
      </ul>
    </li>

    <li><strong>Analyze the Threat</strong>
      <ul>
        <li>Identify enemy disposition, composition, and strength</li>
        <li>Assess capabilities and recent TTPs (Tactics, Techniques, and Procedures)</li>
        <li>Understand enemy doctrine and objectives</li>
        <li>Determine enemy Centers of Gravity (COGs)</li>
      </ul>
    </li>

    <li><strong>Evaluate the Threat COAs (Courses of Action)</strong>
      <ul>
        <li>Develop Most Likely COA (MLCOA) and Most Dangerous COA (MDCOA)</li>
        <li>Identify movement timelines and engagement areas</li>
        <li>Sketch/model enemy formations based on COAs</li>
      </ul>
    </li>

    <li><strong>Develop the SITEMP</strong>
      <ul>
        <li>Create a graphical representation of the MLCOA</li>
        <li>Include:
          <ul>
            <li>Enemy positions and movement routes</li>
            <li>Engagement areas and kill zones</li>
            <li>Key terrain and decision points</li>
            <li>Range fans and EW/cyber overlays if needed</li>
          </ul>
        </li>
      </ul>
    </li>

    <li><strong>Validate and Refine</strong>
      <ul>
        <li>Cross-check with updated intelligence</li>
        <li>Conduct red-cell or war-gaming to test assumptions</li>
        <li>Refine based on commander or staff feedback</li>
      </ul>
    </li>

    <li><strong>Disseminate</strong>
      <ul>
        <li>Brief the commander and staff during MDMP</li>
        <li>Include SITEMP in the IPB Annex of the OPORD</li>
        <li>Ensure access for S2/S3 and maneuver elements</li>
      </ul>
    </li>
  </ul>
</plan>

Guidelines for Execution:
1. Seek Clarification When Needed: If the request is unclear, ask the user clarifying questions before generating a plan.
2. Format the Plan Properly: Once you have enough details, structure the plan within <plan> tags and present it for review.
3. Revise as Requested: If the user requests modifications, update the plan accordingly and present the revised version.
4. Acknowledge Completion: If the user accepts the plan, let them know that we are executing on the plan and add |DONE| at the end of your response.
5. Every time you generate a plan, output the corresponding task inside <task> tags.
"""

    if data.get("question", {}).get("productType", {}).get("template_path"):
        general_context += "\n\nI am going to give you a sample HTML template for the final product. Please generate the plan accordingly."
        template = get_template(data['question']['productType']['template_path'])

        if template:
            general_context += f"\nHere is the sample template inside <html> tags:\n{template}\n"

    plan_type = "custom"
    project_id = data["projectId"]
    if project_id == "default":
        project_id = f"default-{data['userId']}"

    if docs_context:
        general_context += f"\n\nHere are some contexts which might be relevant for the plan. Each context is enclosed in a <context> tag. Use them if they are relevant for the plan.\n{docs_context}"

    try:
        session_prompt = []
        for s in data["session"]:
            session_prompt.append(s[0])
            session_prompt.append(s[1])
        session_prompt.append({"role": "user", "content": prompt})

        if len(session_prompt) >= 6:
            session_prompt.pop(0)

        text = llm.invoke_vllm_text([{"role": "user", "content": general_context}] + session_prompt)
        text = re.sub(r"Anthropic PBC|Anthropic", "Pytho AI", text)
        text = text.replace("Claude", "Ares")

        session_prompt.append({"role": "assistant", "content": text})
        data["session"].append([{"role": "user", "content": prompt}, {"role": "assistant", "content": text}, {}])

        task_result = process_task(text)
        text = task_result["text"]

        plan_result = process_plan(text)
        steps = plan_result["steps"]
        text = plan_result["text"]

        if steps:
            data["plan"] = {
                "task": task_result["task"],
                "productType": data["question"].get("productType"),
                "type": plan_type,
                "steps": steps,
                "dataSources": ["OSINT", "HUMINT", "SIGINT", "JOINT DATABASE"],
            }
            text = text.replace("|DONE|", "")

        if "|DONE|" in text:
            text = re.sub(r"<plan>((.|\n)*?)</plan>", "", text)
            
        data["answer"] = text
        if not steps:
            data["plan"] = None

    except Exception as e:
        print(e)
        data["answer"] = "Sorry! Please try again later."

    save(data)

async def send_agent_step(data, message, should_persist, manager, websocket):
    db = database.SessionLocal()

    display_blob = {
        "title": data.get("title"),
        "type": data.get("type"),
        "executedStepIndex": data.get("executedStepIndex"),
        "avatar": data.get("avatar"),
        "link": data.get("link"),
        "content": data.get("content"),
    }

    if should_persist:
        project_id = f"default-{data['userId']}" if data.get("projectId") == "default" else data.get("projectId")
        created_at = int(time.time() * 1000)  # Current Unix timestamp (ms)
        session_id = data["sessionId"] 

        session_detail_data = {
            "userId": data['userId'],
            "projectId": project_id,
            "sessionId": session_id,
            "timestamp": created_at,
             **display_blob 
        }
        
        print("reached db session detail creation")
        session_detail = database.SessionDetail(
            session_id=session_id,
            timestamp=created_at,
            project_id=project_id,
            data=database.null_safe_json_dumps(session_detail_data)
        )
        db.add(session_detail)
        db.commit()
    
    print(message)
    await manager.send_personal_message(json.dumps(display_blob), websocket)    

    


async def execute(data: Dict[str, Any], docs_context, manager, websocket):
    print(data["plan"])

    # Build the step list string
    steps = "\n".join([f"{i + 1}. {step}" for i, step in enumerate(data["plan"]["steps"])])

    # Base context prompt
    general_context = f"""You are a report generator designed to assist with intelligence tasks.

Your assigned task is:

{data["plan"]["task"]}

To complete this task, follow the designated steps:

{steps}

Here are the requirements: 
- For each step, generate a short outcome for that step.
- The outcome must be in HTML format.
- Wrap your answer inside <html> tags.
 
Ensure accuracy, clarity, and structured formatting in your responses.
"""

    # Append document context if provided
    if docs_context:
        general_context += f"""

Here are some contexts for all the following steps. Each context is enclosed in a <context> tag. Use them if they are relevant for the step.
{docs_context}
"""

    print(general_context)

    session_prompt: List[Dict[str, str]] = []
    combined_text = ""

    try:
        # -------------------------------
        # STEP EXECUTION LOOP
        # -------------------------------
        for i, step in enumerate(data["plan"]["steps"]):
            data["type"] = "executing"
            data["content"] = ""
            data["title"] = step
            data["executedStepIndex"] = i

            await send_agent_step(data, "", False, manager, websocket)
            
            raw_answer = ""

            # Construct per-step message
            session_prompt.append({
                "role": "user",
                "content": f"""
Here are the requirements: 
- For each step, generate a short outcome for that step.
- The outcome must be in HTML format.
- Wrap your answer inside <html> tags.

Now, Execute step {i + 1}. {step}"""
            })

            text = llm.invoke_vllm_text(
                [{"role": "system", "content": general_context}] + session_prompt
            )

            text = (
                text
                .replace("Anthropic PBC", "Pytho AI")
                .replace("Anthropic", "Pytho AI")
                .replace("Claude", "Ares")
                .replace("tags:", "")
            )

            session_prompt.append({"role": "assistant", "content": text})
            raw_answer = text

            # Extract HTML content
            output_pattern = re.compile(r"<html>((.|\n)*?)<\/html>", re.MULTILINE)
            matches = list(output_pattern.finditer(text))
            if matches:
                text = matches[0].group(1)

            text = text.replace("<summary>", "")
            combined_text += text
            data["content"] = text
            data["type"] = "execution_stop"

            message = f"""USER_ID:{data['email']}

Task:
{data['plan']['task']}

Step: {i + 1}. {step}

ANSWER:

{raw_answer}
"""
            await send_agent_step(data, message, True, manager, websocket)

        # -------------------------------
        # CUSTOM PRODUCT LOGIC
        # -------------------------------
        data["title"] = ""
        data["executedStepIndex"] = len(data["plan"]["steps"])

        if data["plan"]["type"] == "custom":
            prompt = ""
            product_template = get_template(data['plan']['productType']['template_path'])
            
            if product_template:
                prompt += f"""
"I am going to give you a sample HTML template for the question I am going to ask next."
Here is the sample template inside <html> tags: 
{product_template}
"""

            if docs_context:
                prompt += f"""

Here are some contexts for the question I am going to ask next. Each context is enclosed in a <context> tag.
{docs_context}
"""

            session_prompt.append({"role": "user", "content": prompt})

            text = llm.invoke_vllm_text(
                [{"role": "system", "content": ""}] + session_prompt
            )

            html_pattern = re.compile(r"[\s\n\r]*<html>((.|\n)*?)<\/html>[\s\n\r]*", re.MULTILINE)
            html_matches = list(html_pattern.finditer(text))
            html_body = ""
            num_tries = 0

            while num_tries < 5 and not html_matches:
                print("\nIncomplete HTML ... trying again...\n")

                session_prompt.append({
                    "role": "user",
                    "content": "Please complete the HTML. Start exactly where you left off.\nDo not start with any extra words, for example, 'Certainly!'"
                })

                text += llm.invoke_vllm_text([{"role": "system", "content": ""}] + session_prompt)
                session_prompt.pop()  # remove user request
                session_prompt.pop()  # remove partial html
                session_prompt.append({"role": "assistant", "content": text})

                html_matches = list(html_pattern.finditer(text))
                num_tries += 1

            if not html_matches:
                html_body = text[text.find("<html>") + len("<html>") :]
            else:
                html_body = html_matches[0].group(1)

            if product_template:  # Replace the template's body with the generated text. Keep the <header>
                with open(data['plan']['productType']['template_path'], "r", encoding="utf-8") as file:
                    product_template = file.read()
                text = re.sub(template_pattern, f"<body>{html_body}</body>", product_template)
            else:
                text = f"<html><body>{html_body}</body></html>"

            file_key = f"{int(time.time())}-{random.randint(1, 1000)}"
            html_path = os.path.join(config.Config.UPLOAD_DIR(), f"{file_key}.html")
            html_link = FILE_HOST_API + f"{file_key}.html"

            with open(html_path, "w", encoding="utf-8") as file:
                file.write(text)
            text = f'<a href="{html_link}" style="color:#90caf9;" target="_blank">Here</a> is the {data["plan"]["productType"]["productName"]} you asked for.'

            data["content"] = text
            data["link"] = html_link
            data["type"] = "execution_combined"
            await send_agent_step(data, data.get("content") + '\n\n' + data["link"], True, manager, websocket)
            

    except Exception as e:
        print(e)
        import traceback
        traceback.print_exc()

        data["type"] = "execution_combined"
        data["content"] = "Sorry! Please try again later."
        await send_agent_step(data, e + '\n\n' + data.get("content"), False, manager, websocket)
       

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


def get_context(question, previous_context, context_count=4):
    matched_docs = search.top_k_similar_chunks(question, context_count)

    context_log = previous_context.get('context_log', '') + '\nCONTEXT\n\n'
    sources_map = previous_context.get('sources_map', {})
    context = previous_context.get('context', '')
    previous_chunks = {(d["doc_id"], d["chunk_id"]) for d in sources_map.values()}

    for doc in matched_docs:
        id = len(sources_map)
           
        if (doc["doc_id"], doc["chunk_id"]) not in previous_chunks and doc["score"] > 0.61:
            
            context_log += f'id: {id}\nsimilarity:{doc["score"]}\n{doc["link"]}\n\n'
            content = doc['content'].strip()
            sources_map[str(id)] = {
                'id': id,
                'title': doc['title'],
                'linkLabel': "Page " + str(doc['page']),
                'link': doc['link'],
                'doc_id': doc['doc_id'],
                'chunk_id': doc['chunk_id'],
            }
            
            context += f"""

<context>
  {content}
  <id>{id}</id>
</context>"""

        else:
            context_log += f'id: {id}\nsimilarity:{doc["score"]} <dropped>\n\n'            

    return {
        'sources_map': sources_map,
        'context': context,
        'context_log': context_log
    }


def doc_chat(data):
    # Build the system prompt
    prompt = f"""You are a specialized agent skilled in HTML document manipulation. I will provide:

The full HTML document, and
A context within the document.

Your task is to modify the context based on the provided document.

Inputs:

HTML Document:
{data.get('document', '')}

Context:
{data.get('context', '')}

Requirements:

1. Analyze and answer based on the given context and HTML document.
2. Return your response in valid HTML format.
3. Preserve all original formatting, links, and styles from the context.

Now, answer this question: {data.get('question', '')}
"""
    print(prompt)

    try:
        # Call the LLM (assumed async)
        text = llm.invoke_vllm_text([
            # {"role": "system", "content": ""},
            {"role": "user", "content": prompt}
        ])

        # Clean and normalize model response
        text = (
            text
            .replace("Anthropic PBC", "Pytho AI")
            .replace("Anthropic", "Pytho AI")
            .replace("Claude", "Ares")
        )

        print(text)

        # Parse returned HTML
        soup = BeautifulSoup(text, "html.parser")
        extracted = []

        # Extract all top-level HTML elements under <body>
        body = soup.body or soup
        for el in body.contents:
            if getattr(el, "name", None):
                extracted.append(str(el))

        # Join HTML fragments into a single string
        data["answer"] = "".join(extracted)

        if len(data["answer"]) < 1:
            data["errorMessage"] = "Missing HTML"


    except Exception as e:
        print(e)
        import traceback
        traceback.print_exc()

        data["errorMessage"] = "Sorry! Please try again later."
        data["answer"] = "Sorry! Please try again later."


async def main(data, manager, websocket):
    print(data)
    logging.info('main agent')
    question = str(data.get('question', {}).get('prompt', '') or '').replace('\n', ' ')
    user_id = data['userId']
    alert_log = f'{user_id}\nQUESTION: {question}\n' 
    context_log = ''
    docs_context = ''
    sources_map = {}
    
    text = 'Something went wrong!'
    data['title'] = question
    data['timestamp'] = math.floor(time.time() * 1000)
    
    try:
        
        if len(data['docs']) > 0:
            context_result = get_context_from_docs(data['docs'], data.get('docsSummary'))
            docs_context = context_result['context']
            sources_map.update(context_result['sources_map'])
            context_log += context_result['context_log']
            data['docsSummary'] = context_result.get('docs_summary')
            
        if (data.get("type") == 'doc_chat'):
            doc_chat(data)
            await manager.send_personal_message(json.dumps(data), websocket)
            return
    
        if (data.get("type") == 'plan'):
            plan(data, docs_context)
            await manager.send_personal_message(json.dumps(data), websocket)
            return
        
        if (data.get("type") == 'execute'):
            await execute(data, docs_context, manager, websocket)
            return
    
  
    except Exception as e:
        logging.exception(f'user: {user_id}\nquestion:\n{question}\n')
        alert_log += str(e)

    # data['answer'] = text.encode().decode("unicode-escape")
    data['answer'] = text.strip()
    alert_log += f'\n\nANSWER: {data["answer"]}'
    logging.info(context_log)
    print("logging info", context_log)
    save(data)
    print("done")
    # try:
    #   slack_client.chat_postMessage(channel='pytho-chats-demo',text=alert_log)
    # except Exception as e:
    #   print()  
    await manager.send_personal_message(json.dumps(data), websocket)
            