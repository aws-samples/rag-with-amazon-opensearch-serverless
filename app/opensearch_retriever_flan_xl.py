import os
import json
import logging
import sys
from typing import List
from urllib.parse import urlparse

import boto3

from langchain_community.vectorstores import OpenSearchVectorSearch
from langchain_community.embeddings import SagemakerEndpointEmbeddings
from langchain_community.embeddings.sagemaker_endpoint import EmbeddingsContentHandler

from langchain.llms.sagemaker_endpoint import (
    SagemakerEndpoint,
    LLMContentHandler
)

from langchain.prompts import PromptTemplate
from langchain.chains import RetrievalQA

from opensearchpy import (
    AWSV4SignerAuth,
    RequestsHttpConnection
)

logger = logging.getLogger()
logging.basicConfig(format='%(asctime)s,%(module)s,%(processName)s,%(levelname)s,%(message)s', level=logging.INFO, stream=sys.stderr)

CONNECTION_TIMEOUT = 1000


class SagemakerEndpointEmbeddingsJumpStart(SagemakerEndpointEmbeddings):
    def embed_documents(
        self, texts: List[str], chunk_size: int = 5
    ) -> List[List[float]]:
        """Compute doc embeddings using a SageMaker Inference Endpoint.

        Args:
            texts: The list of texts to embed.
            chunk_size: The chunk size defines how many input texts will
                be grouped together as request. If None, will use the
                chunk size specified by the class.

        Returns:
            List of embeddings, one for each text.
        """
        results = []

        _chunk_size = len(texts) if chunk_size > len(texts) else chunk_size
        for i in range(0, len(texts), _chunk_size):
            response = self._embedding_func(texts[i : i + _chunk_size])
            results.extend(response)
        return results


def _create_sagemaker_embeddings(endpoint_name: str, region: str = "us-east-1") -> SagemakerEndpointEmbeddingsJumpStart:

    class ContentHandlerForEmbeddings(EmbeddingsContentHandler):
        """
        encode input string as utf-8 bytes, read the embeddings
        from the output
        """
        content_type = "application/json"
        accepts = "application/json"
        def transform_input(self, prompt: str, model_kwargs = {}) -> bytes:
            input_str = json.dumps({"text_inputs": prompt, **model_kwargs})
            return input_str.encode('utf-8')

        def transform_output(self, output: bytes) -> str:
            response_json = json.loads(output.read().decode("utf-8"))
            embeddings = response_json["embedding"]
            if len(embeddings) == 1:
                return [embeddings[0]]
            return embeddings

    # create a content handler object which knows how to serialize
    # and deserialize communication with the model endpoint
    content_handler = ContentHandlerForEmbeddings()

    # read to create the Sagemaker embeddings, we are providing
    # the Sagemaker endpoint that will be used for generating the
    # embeddings to the class
    embeddings = SagemakerEndpointEmbeddingsJumpStart(
        endpoint_name=endpoint_name,
        region_name=region,
        content_handler=content_handler
    )
    logger.info(f"embeddings type={type(embeddings)}")

    return embeddings


def _get_auth(region_name: str) -> AWSV4SignerAuth:
    """
    Get AWSV4SignerAuth to access Amazon OpenSearch Serverless
    """
    credentials = boto3.Session(region_name=region_name).get_credentials()
    auth = AWSV4SignerAuth(credentials, region_name, 'aoss')

    return auth


def build_chain():
    region = os.environ["AWS_REGION"]
    opensearch_domain_endpoint = os.environ["OPENSEARCH_DOMAIN_ENDPOINT"]
    opensearch_index = os.environ["OPENSEARCH_INDEX"]
    embeddings_model_endpoint = os.environ["EMBEDDING_ENDPOINT_NAME"]
    text2text_model_endpoint = os.environ["TEXT2TEXT_ENDPOINT_NAME"]

    class ContentHandler(LLMContentHandler):
        content_type = "application/json"
        accepts = "application/json"

        def transform_input(self, prompt: str, model_kwargs: dict) -> bytes:
            input_str = json.dumps({"text_inputs": prompt, **model_kwargs})
            return input_str.encode('utf-8')

        def transform_output(self, output: bytes) -> str:
            response_json = json.loads(output.read().decode("utf-8"))
            return response_json["generated_texts"][0]

    content_handler = ContentHandler()

    model_kwargs = {
      "max_length": 500,
      "num_return_sequences": 1,
      "top_k": 250,
      "top_p": 0.95,
      "do_sample": False,
      "temperature": 1
    }

    llm = SagemakerEndpoint(
        endpoint_name=text2text_model_endpoint,
        region_name=region,
        model_kwargs=model_kwargs,
        content_handler=content_handler
    )

    opensearch_url = f"https://{opensearch_domain_endpoint}" if not opensearch_domain_endpoint.startswith('https://') else opensearch_domain_endpoint
    http_auth = _get_auth(region)

    opensearch_vector_search = OpenSearchVectorSearch(
        opensearch_url=opensearch_url,
        index_name=opensearch_index,
        embedding_function=_create_sagemaker_embeddings(embeddings_model_endpoint, region),
        http_auth=http_auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=CONNECTION_TIMEOUT
    )

    retriever = opensearch_vector_search.as_retriever(search_kwargs={"k": 3})

    prompt_template = """Answer based on context:\n\n{context}\n\n{question}"""

    PROMPT = PromptTemplate(
        template=prompt_template, input_variables=["context", "question"]
    )

    chain_type_kwargs = {"prompt": PROMPT, "verbose": True}
    qa = RetrievalQA.from_chain_type(
        llm,
        chain_type="stuff",
        retriever=retriever,
        chain_type_kwargs=chain_type_kwargs,
        return_source_documents=True,
        verbose=True, #DEBUG
    )

    logger.info(f"\ntype('qa'): \"{type(qa)}\"\n")
    return qa


def run_chain(chain, prompt: str, history=[]):
    result = chain(prompt, include_run_info=True)
    # To make it compatible with chat samples
    return {
        "answer": result['result'],
        "source_documents": result['source_documents']
    }


if __name__ == "__main__":
    chain = build_chain()
    result = run_chain(chain, "What is SageMaker model monitor? Write your answer in a nicely formatted way.")
    print(result['answer'])
    if 'source_documents' in result:
        print('Sources:')
        for d in result['source_documents']:
          print(d.metadata['source'])