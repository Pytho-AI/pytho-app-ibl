"""File handler utilities for S3 operations and document loading."""
import boto3
from botocore.exceptions import ClientError
from urllib.parse import urlparse, unquote
from langchain_community.document_loaders import PyPDFLoader, Docx2txtLoader, TextLoader
import tempfile
import os
import logging
from typing import Optional

# Initialize S3 client
s3_client = boto3.client('s3', region_name='us-east-1')


def parse_s3_url(url: str) -> tuple:
    """Parse an S3 URL and extract bucket and key."""
    parsed = urlparse(url)
    
    # Extract bucket from hostname
    hostname = parsed.netloc
    bucket = hostname.split('.s3.')[0]
    
    # Extract key from path
    key = unquote(parsed.path.lstrip('/'))
    
    return bucket, key


def generate_fresh_s3_url(bucket: str, key: str, expiration: int = 3600) -> str:
    """Generate a fresh pre-signed URL for an S3 object."""
    return s3_client.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=expiration
    )


def find_bucket_for_file(file_key: str) -> Optional[str]:
    """Try to find which S3 bucket contains a file by checking all available buckets."""
    s3_key = f"public/{file_key}"
    try:
        # List buckets and check which one contains the file
        response_buckets = s3_client.list_buckets()
        for bucket_info in response_buckets.get('Buckets', []):
            bucket_name = bucket_info['Name']
            try:
                s3_client.head_object(Bucket=bucket_name, Key=s3_key)
                logging.info(f"Found file in bucket: {bucket_name}")
                return bucket_name
            except ClientError:
                continue
    except Exception as e:
        logging.warning(f"Error finding bucket for file: {e}")
    
    return None


def download_s3_file(bucket: str, key: str, local_path: str) -> None:
    """Download a file from S3 to a local path."""
    s3_client.download_file(bucket, key, local_path)


def load_document_pages(file_key: str, bucket: str, fresh_url: str) -> list:
    """
    Load document pages from a file.
    Supports PDF, DOCX, and TXT files.
    
    Returns a list of document pages or empty list if unsupported/error.
    """
    pages = []
    
    if file_key.lower().endswith(".pdf"):
        loader = PyPDFLoader(fresh_url)
        pages = loader.load_and_split()
        
    elif file_key.lower().endswith(".docx") or file_key.lower().endswith(".txt"):
        suffix = ".docx" if file_key.lower().endswith(".docx") else ".txt"
        s3_key = f"public/{file_key}"
        
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
            tmp_path = tmp_file.name
            download_s3_file(bucket, s3_key, tmp_path)
        
        try:
            if file_key.lower().endswith(".docx"):
                loader = Docx2txtLoader(tmp_path)
            else:
                loader = TextLoader(tmp_path)
            pages = loader.load_and_split()
        finally:
            os.unlink(tmp_path)
    
    return pages


def get_context_from_attachments(attachments, bucket: str, attachment_type: str = "RFI") -> dict:
    """
    Extract context from attachments for LLM processing.
    
    Args:
        attachments: List of attachment objects with file_key and file_name attributes
        bucket: S3 bucket name
        attachment_type: Type label for the attachments (e.g., "RFI", "Response#1")
    
    Returns:
        dict with keys: sources_map, context, context_log
    """
    context_log = ""
    sources_map = {}
    context_temp = ""
    
    if not bucket:
        logging.warning("No S3 bucket available, cannot load attachments")
        return {"sources_map": {}, "context": "", "context_log": "No S3 bucket configured\n"}
    
    for d, attachment in enumerate(attachments):
        file_key = attachment.file_key
        file_name = attachment.file_name
        
        if not file_key:
            continue
        
        try:
            # S3 key is public/{file_key} based on aws-amplify upload settings
            s3_key = f"public/{file_key}"
            logging.info(f"Processing {attachment_type} attachment: bucket={bucket}, key={s3_key}")
            
            # Generate fresh pre-signed URL
            fresh_url = generate_fresh_s3_url(bucket, s3_key)
            
            # Load document pages
            pages = load_document_pages(file_key, bucket, fresh_url)
            
            if not pages:
                if not (file_key.lower().endswith(".pdf") or 
                        file_key.lower().endswith(".docx") or 
                        file_key.lower().endswith(".txt")):
                    logging.warning(f"Unsupported file type: {file_key}")
                    context_log += f"Skipped unsupported file type: {file_key}\n"
                continue
            
            context_log += f"Using {len(pages)} pages from {attachment_type} attachment: {file_name}\n"
            
            context_temp += f"\nFollowing <context>s are from {attachment_type} attachment '{file_name}':\n"
            for i, page in enumerate(pages):
                id = f"{attachment_type.lower()}_att.{d}#page={i+1}"
                sources_map[id] = {
                    "id": id,
                    "link": fresh_url + f"#page={i+1}",
                    "title": f"{attachment_type} Attachment: {file_name}",
                    "linkLabel": f"Page {i+1}"
                }
                
                page_context = f"""

<context>
  {page.page_content.strip()}
  <id>{id}</id>
</context>"""
                context_temp += page_context

            context_temp += f"\nEnd of {attachment_type} attachment '{file_name}'\n"
            
        except ClientError as e:
            logging.error(f"S3 error for {attachment_type} attachment {file_key}: {e}")
            context_log += f"Error accessing S3 for {file_key}\n"
        except Exception as e:
            logging.error(f"Error processing {attachment_type} attachment {file_key}: {e}", exc_info=True)
            context_log += f"Error processing {file_key}: {str(e)}\n"
    
    return {
        "sources_map": sources_map,
        "context": context_temp,
        "context_log": context_log
    }

