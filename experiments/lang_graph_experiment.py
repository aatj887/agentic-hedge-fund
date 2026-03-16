from dotenv import load_dotenv, find_dotenv
import pandas as pd
import os
from datetime import date
from openbb import obb
from typing import Annotated, Sequence, TypedDict
from langchain_core.messages import BaseMessage, ToolMessage, SystemMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import START, END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

load_dotenv(find_dotenv())

class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], add_messages]

@tool
def get_historical_prices(symbol:str, start_date:str, end_date:str) -> pd.DataFrame:
    """
    Returns a dataframe with the columns 'open', 'high', 'low', 'close', 'volume', 'split_ratio', 'dividend',
       'adjClose'.
    Args:
        symbol (str): Symbol or ticker of the company
        start_date (str): Start date in the 'YYYY-MM-DD' format
        end_date (str): End date in the 'YYYY-MM-DD' format

    Returns:
        pd.DataFrame: A dataframe containing the data explained in the summary, with the date as an index.
    """
    
    data = obb.equity.price.historical(
        symbol=symbol, 
        start_date=start_date, 
        end_date=end_date,
        adjustment='splits_and_dividends', # Makes it so it includes all columns
        provider='yfinance' # Fixed so the output is known and consistent.
    ).to_df()
    
    return data

@tool
def get_today_date() -> str:
    """
    Returns todays date as a string in the 'YYYY-MM-DD' format.
    Args:
        None.
    Returns:
        str: todays date as a string in the 'YYYY-MM-DD' format.
    """
    return date.today().strftime('%Y-%m-%d')
    

tools = [get_historical_prices, get_today_date]

model = ChatGoogleGenerativeAI(
    model='gemini-3.1-flash-lite-preview',
    api_key = os.environ['GOOGLE_API_KEY']
    ).bind_tools(tools)

def model_call(state:AgentState) -> AgentState:
    system_prompt = SystemMessage(content='You are my Finance-Savvy AI assistant, please answer my query using only exact information.')
    
    response = model.invoke([system_prompt] + state['messages'])
    return {'messages': [response]}

def should_continue(state: AgentState) -> AgentState:
    messages = state['messages']
    last_message = messages[-1]
    if not last_message.tool_calls:
        return 'end'
    
    else:
        return 'continue'

graph = StateGraph(AgentState)
graph.add_node('our_agent', model_call)

tool_node = ToolNode(tools=tools)
graph.add_node('tools', tool_node)

graph.set_entry_point('our_agent')

graph.add_conditional_edges(
    'our_agent',
    should_continue,
    {
        'continue': 'tools',
        'end': END
    }
)
graph.add_edge('tools', 'our_agent')

app = graph.compile()

def print_stream(stream):
    for s in stream:
        message = s['messages'][-1]
        
        # Check if the message content is a list of dictionaries (common in structured outputs)
        if isinstance(message.content, list) and len(message.content) > 0:
            # Extract the 'text' field from the first dictionary
            # This handles the format: [{'type': 'text', 'text': '...', 'extras': ...}]
            print(message.content[0].get('text', message.content))
        elif isinstance(message, tuple):
            print(message)
        else:
            # Fallback for standard messages or simple strings
            message.pretty_print()
            
inputs = {'messages': [('user', 'What were the last three closing prices for AAPL?')]}
print_stream(app.stream(inputs, stream_mode='values'))

    