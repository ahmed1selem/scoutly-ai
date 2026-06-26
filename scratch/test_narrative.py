import asyncio
import json
from pathlib import Path
import sys
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from backend.data_pipeline.enrich_profiles import generate_narrative
from backend.utils.config import settings
from langchain_openai import AzureChatOpenAI

async def main():
    d = json.load(open('E:/ITI/agentic/project/KB/profiles_all_top5_2025_26.json', encoding='utf-8'))['profiles'][0]
    llm = AzureChatOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_ENDPOINT,
        azure_deployment=settings.AZURE_OPENAI_DEPLOYMENT_NAME,
        api_key=settings.AZURE_OPENAI_API_KEY,
        api_version=settings.AZURE_OPENAI_API_VERSION,
        temperature=0.3
    )
    res = await generate_narrative(llm, d)
    print(res)

if __name__ == '__main__':
    asyncio.run(main())
