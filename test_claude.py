import os
import traceback
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage

print("Testing ChatAnthropic...")
try:
    llm = ChatAnthropic(model='claude-4-6-sonnet-latest', max_tokens=10)
    print(llm.invoke([HumanMessage(content='hi')]))
except Exception as e:
    traceback.print_exc()
