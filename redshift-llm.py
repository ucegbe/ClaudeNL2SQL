import json
import streamlit as st
import time
import boto3
import pandas as pd
from anthropic import Anthropic
CLAUDE = Anthropic()
import multiprocessing
import subprocess
import shutil
import os
import codecs
from botocore.exceptions import ClientError
import uuid
import pandas as pd
from io import StringIO
import re

REDSHIFT=boto3.client('redshift-data')
with open('config.json') as f:
    config_file = json.load(f)
CLUSTER_IDENTIFIER=config_file["redshift-identifier"]
DATABASE = config_file["database-name"]
DB_USER =config_file["database-user"]
SERVERLESS=config_file['serverless']
DEBUGGING_MAX_RETRIES=config_file['debug-max-retries']

ATHENA=boto3.client('athena')
GLUE=boto3.client('glue')
S3=boto3.client('s3')
prompt_path="prompt"
MIXTRAL_ENPOINT="mixtral"


with open('pricing.json') as f:
    pricing_file = json.load(f)

from botocore.config import Config
config = Config(
    read_timeout=600,
    retries = dict(
        max_attempts = 5
    )
)
BEDROCK=boto3.client(service_name='bedrock-runtime',region_name='us-east-1',config=config)
bedrock_runtime=boto3.client(service_name='bedrock-runtime',region_name='us-east-1',config=config)
st.set_page_config(page_icon=None, layout="wide")

if 'input_token' not in st.session_state:
    st.session_state['input_token'] = 0
if 'output_token' not in st.session_state:
    st.session_state['output_token'] = 0
if 'action_name' not in st.session_state:
    st.session_state['action_name'] = ""
if 'messages' not in st.session_state:
    st.session_state['messages'] = []
if 'cost' not in st.session_state:
    st.session_state['cost'] = 0


    
import json

@st.cache_resource
def token_counter(path):
    tokenizer = LlamaTokenizer.from_pretrained(path)
    return tokenizer

@st.cache_resource
def mistral_counter(path):
    tokenizer = AutoTokenizer.from_pretrained(path)
    return tokenizer

@st.cache_data
def get_tables_redshift(identifier, database,  schema,serverless,db_user=None,):
    if serverless:
        tables_ls = REDSHIFT.list_tables(
       WorkgroupName=identifier,
        Database=database,
        SchemaPattern=schema
        )
    else:
        tables_ls = REDSHIFT.list_tables(
        ClusterIdentifier=identifier,
        Database=database,
        DbUser=db_user,
        SchemaPattern=schema
        )
    return [x['name'] for x in  tables_ls['Tables']]

@st.cache_data
def get_db_redshift(identifier, database,serverless, db_user=None, ):
    if serverless:
        db_ls = REDSHIFT.list_databases(
        WorkgroupName=identifier,
        Database=database,
        )    
    else:
        db_ls = REDSHIFT.list_databases(
        ClusterIdentifier=identifier,
        Database=database,
        DbUser=db_user
        )
    return db_ls['Databases']

@st.cache_data
def get_schema_redshift(identifier, database, serverless, db_user=None,):
    if serverless:
        schema_ls = REDSHIFT.list_schemas(
        WorkgroupName=identifier,
        Database=database,

        )
    else:        
        schema_ls = REDSHIFT.list_schemas(
        ClusterIdentifier=identifier,
        Database=database,
        DbUser=db_user
        )
    return schema_ls['Schemas']

@st.cache_data
def execute_query_redshyft(sql_query, identifier, database, serverless,db_user=None):
    if serverless:
        response = REDSHIFT.execute_statement(
        WorkgroupName=identifier,
            Database=database,

            Sql=sql_query
        )
    else:        
        response = REDSHIFT.execute_statement(
            ClusterIdentifier=identifier,
            Database=database,
            DbUser=db_user,
            Sql=sql_query
        )
    return response

def single_execute_query(params,sql_query, identifier, database, question,serverless,db_user=None):
    response = execute_query_redshyft(sql_query, identifier, database,serverless, db_user)
    df=redshyft_querys(sql_query,response,question,params,identifier, 
                       database,                     
                       question,
                         db_user,)    
    return df


def execute_query_with_pagination( sql_query, identifier, database,  serverless, db_user=None,):
    results_list=[]
    if serverless:
        response_b = REDSHIFT.batch_execute_statement(
            WorkgroupName=identifier,
            Database=database,
        
            Sqls=sql_query
        ) 
    else:
        response_b = REDSHIFT.batch_execute_statement(
            ClusterIdentifier=identifier,
            Database=database,
            DbUser=db_user,
            Sqls=sql_query
        )   
    describe_b=REDSHIFT.describe_statement(
         Id=response_b['Id'],
    )       
    status=describe_b['Status']
    while status != "FINISHED":
        time.sleep(1)
        describe_b=REDSHIFT.describe_statement(
                         Id=response_b['Id'],
                    ) 
        status=describe_b['Status']
    max_attempts = 5 
    attempts = 0
    while attempts < max_attempts:
        try:
            for ids in describe_b['SubStatements']:
                result_b = REDSHIFT.get_statement_result(Id=ids['Id'])                
                results_list.append(get_redshift_table_result(result_b))
            break
        except REDSHIFT.exceptions.ResourceNotFoundException as e:
            attempts += 1
            time.sleep(2)
    return results_list

def llm_debugga(question, statement, error, params):
    model="claude" if "claude" in params["sql_model"].lower() else "mixtral" 
    with open(f"{prompt_path}/{params['engine']}/{model}-debugger.txt","r") as f:
        prompts=f.read()
    values = {
    "error":error,
    "sql":statement,
    "schema": params['schema'],
    "sample": params['sample'],
    "question":params['prompt']
    }
    prompts=prompts.format(**values)
    output=query_llm(prompts,params)
    return output

def get_redshift_table_result(response):

    columns = [c['name'] for c in response['ColumnMetadata']] 
    data = []
    for r in response['Records']:
        row = []
        for col in r:
            row.append(list(col.values())[0])  
        data.append(row)
    df = pd.DataFrame(data, columns=columns)    
    return df.to_csv(index=False)

def redshyft_querys(q_s,response,prompt,params,identifier, database, question,db_user=None,): 
    max_execution=5
    debug_count=max_execution
    alert=False
    try:
        statement_result = REDSHIFT.get_statement_result(
            Id=response['Id'],
        )
    except REDSHIFT.exceptions.ResourceNotFoundException as err:  
        describe_statement=REDSHIFT.describe_statement(
             Id=response['Id'],
        )
        query_state=describe_statement['Status']  
        while query_state in ['SUBMITTED','PICKED','STARTED']:
      
            time.sleep(1)
            describe_statement=REDSHIFT.describe_statement(
                 Id=response['Id'],
            )
            query_state=describe_statement['Status']
        while (max_execution > 0 and query_state == "FAILED"):
            max_execution = max_execution - 1
            print(f"\nDEBUG TRIAL {max_execution}")
            bad_sql=describe_statement['QueryString']
            print(f"\nBAD SQL:\n{bad_sql}")                
            error=describe_statement['Error']
            print(f"\nERROR:{error}")
            print("\nDEBUGGIN...")
            cql=llm_debugga(prompt, bad_sql, error, params)            
            idx1 = cql.index('<sql>')
            idx2 = cql.index('</sql>')
            q_s=cql[idx1 + len('<sql>') + 1: idx2]
            print(f"\nDEBUGGED SQL\n {q_s}")
            ### Guardrails to prevent the LLM from altering tables
            if any(keyword in q_s for keyword in ["CREATE", "DROP", "ALTER","INSERT","UPDATE","TRUNCATE","DELETE","MERGE","REPLACE","UPSERT"]):
                alert="I AM NOT PERMITTED TO MODIFY THIS TABLE, CONTACT ADMIN."       
                alert=True
                break
            else:
                response = execute_query_redshyft(q_s, identifier, database,params['serverless'],db_user)
                describe_statement=REDSHIFT.describe_statement(
                                     Id=response['Id'],
                                )
                query_state=describe_statement['Status']
                while query_state in ['SUBMITTED','PICKED','STARTED']:
                    time.sleep(2)            
                    describe_statement=REDSHIFT.describe_statement(
                                     Id=response['Id'],
                                )
                    query_state=describe_statement['Status']
                if query_state == "FINISHED":                
                    break 
        
        if max_execution == 0 and query_state == "FAILED":
            print(f"DEBUGGING FAILED IN {str(debug_count)} ATTEMPTS")
        elif alert:
            pass
        else:           
            max_attempts = 5
            attempts = 0
            while attempts < max_attempts:
                try:
                    time.sleep(1)
                    statement_result = REDSHIFT.get_statement_result(
                        Id=response['Id']
                    )
                    break

                except REDSHIFT.exceptions.ResourceNotFoundException as e:
                    attempts += 1
                    time.sleep(5)
    if max_execution == 0 and query_state == "FAILED":
        df=f"DEBUGGING FAILED IN {str(debug_count)} ATTEMPTS. NO RESULT AVAILABLE"
    elif alert:
        df="I AM NOT PERMITTED TO MODIFY THIS TABLE, CONTACT ADMIN."     
    else:
        df=get_redshift_table_result(statement_result)
    return df, q_s

def redshift_qna(params,stream_handler=None):
    if "tables" in params:
        sql1=f"SELECT table_catalog,table_schema,table_name,column_name,ordinal_position,is_nullable,data_type FROM information_schema.columns WHERE table_schema='{params['db_schema']}'"
        sql2=[]
        for table in params['tables']:
            sql2.append(f"SELECT * from {params['db']}.{params['db_schema']}.{table} LIMIT 10")
        sqls=[sql1]+sql2        
        # st.write(sqls)
        question=params['prompt']
        results=execute_query_with_pagination(sqls, CLUSTER_IDENTIFIER, params['db'], params['serverless'],DB_USER)    
        col_names=results[0].split('\n')[0]
        observations="\n".join(sorted(results[0].split('\n')[1:])).strip()
        params['schema']=f"{col_names}\n{observations}"
        params['sample']=''
        for examples in results[1:]:
            params['sample']+=f"{examples}\n\n"
    elif "table" in params:
        sql1=f"SELECT * FROM information_schema.columns WHERE table_name='{params['table']}' AND table_schema='{params['db_schema']}'"
        sql2=f"SELECT * from {params['db']}.{params['db_schema']}.{params['table']} LIMIT 10"
        question=params['prompt']
        sqls=[sql1]+[sql2] 
        results=execute_query_with_pagination(sqls, CLUSTER_IDENTIFIER, params['db'],params['serverless'], DB_USER)    
        params['schema']=results[0]
        params['sample']=results[1]
    model="claude" if "claude" in params["sql_model"].lower() else "mixtral"
    with open(f"{prompt_path}/{params['engine']}/{model}-sql.txt","r") as f:
        prompts=f.read()
    values = {
    "schema": params['schema'],
    "sample": params['sample'],
    "question": question,
    }
    prompts=prompts.format(**values)
    q_s=query_llm(prompts,params) 
    sql_pattern = re.compile(r'<sql>(.*?)(?:</sql>|$)', re.DOTALL)           
    sql_match = re.search(sql_pattern, q_s)
    q_s = sql_match.group(1)
    
    if any(keyword in q_s for keyword in ["CREATE", "DROP", "ALTER","INSERT","UPDATE","TRUNCATE","DELETE","MERGE","REPLACE","UPSERT"]):
        output="I AM NOT PERMITTED TO MODIFY THIS TABLE, CONTACT ADMIN."
    else:    
        output, q_s=single_execute_query(params,q_s, CLUSTER_IDENTIFIER, params['db'] ,question,params['serverless'],DB_USER)    
    input_token=CLAUDE.count_tokens(output) if "claude" in params['model_id'].lower() else mistral_counter("mistralai/Mixtral-8x7B-v0.1").encode(output)
    if ("claude" in params['model_id'].lower() and input_token>90000) or ("claude" not in params['model_id'].lower() and len(input_token)>28000):    
        csv_rows=output.split('\n')
        st.write("TOKEN TOO LARGE, CHUNKING...")
        chunk_rows=chunk_csv_rows(csv_rows, max_token_per_chunk=80000 if "claude" in params['model_id'].lower() else 20000)
        initial_summary=[]
        for chunk in chunk_rows:            
            model="claude" if "claude" in params['model_id'].lower() else "mixtral"
            with open(f"{prompt_path}/{params['engine']}/{model}-text-gen.txt","r") as f:
                prompts=f.read()
            values = {   
            "sql":q_s,
            "csv": chunk,       
            "question":question,
            }
            prompts=prompts.format(**values)
            # if "claude" == model:
            #     prompts=f"\n\nHuman:\n{prompts}\n\nAssistant:"
            initial_summary.append(summary_llm(prompts, params, None))            

        prompts = f'''You are a helpful assistant. 
Here is a list of answers from multiple subset of a table for a given question:
<multiple_answers>
{initial_summary}
</multiple_answers>
Here is the given question:
{question}
Your job is to merge the multiple answers into a coherent single answer.'''
        if "claude" == model:
            prompts=f"\n\nHuman:\n{prompts}\n\nAssistant:"
        response=summary_llm(prompts, params, stream_handler)     
    else:        
        model="claude" if "claude" in params['model_id'].lower() else "mixtral"
        with open(f"{prompt_path}/{params['engine']}/{model}-text-gen.txt","r") as f:
            prompts=f.read()
        values = {   
        "sql":q_s,
        "csv": output,       
        "question":question,
        }
        prompts=prompts.format(**values)       
        response=summary_llm(prompts, params, stream_handler)
    return response, q_s,output

def bedrock_streemer(response, handler):
    stream = response.get('body')
    answer = ""
    i = 1
    if stream:
        for event in stream:
            chunk = event.get('chunk')
            if  chunk:
                chunk_obj = json.loads(chunk.get('bytes').decode())
                if "delta" in chunk_obj:                    
                    delta = chunk_obj['delta']
                    if "text" in delta:
                        text=delta['text']                         
                        answer+=str(text)
                        if handler:
                            handler.markdown(answer)
                        i+=1
                if "amazon-bedrock-invocationMetrics" in chunk_obj:
                    input_tokens= chunk_obj['amazon-bedrock-invocationMetrics']['inputTokenCount']
                    output_tokens=chunk_obj['amazon-bedrock-invocationMetrics']['outputTokenCount']
                    print(f"\nInput Tokens: {input_tokens}\nOutput Tokens: {output_tokens}")
    return answer,input_tokens, output_tokens

def bedrock_claude_(chat_history,params, prompt, handler=None):
    content=[{
        "type": "text",
        "text": prompt
            }]
    chat_history.append({"role": "user",
            "content": content})
    prompt = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": params['sql_token'],
        "temperature": params["temp"],
        "system":params["system_message"],
        "messages": chat_history
    }
    answer = ""
    prompt = json.dumps(prompt)  
    response = bedrock_runtime.invoke_model_with_response_stream(body=prompt, modelId=params["sql_model"], accept="application/json", contentType="application/json")
    answer,input_tokens,output_tokens=bedrock_streemer(response, handler) 
    return answer, input_tokens, output_tokens

def _invoke_bedrock_with_retries(chat_history, params, question, handler=None):
    max_retries = 5
    backoff_base = 2
    max_backoff = 3  # Maximum backoff time in seconds
    retries = 0

    while True:
        try:
            response,input_tokens,output_tokens= bedrock_claude_(chat_history, params, question, handler)
            return response,input_tokens,output_tokens
        except ClientError as e:
            if e.response['Error']['Code'] == 'ThrottlingException':
                if retries < max_retries:
                    # Throttling, exponential backoff
                    sleep_time = min(max_backoff, backoff_base ** retries + random.uniform(0, 1))
                    time.sleep(sleep_time)
                    retries += 1
                else:
                    raise e
            elif e.response['Error']['Code'] == 'ModelStreamErrorException':
                if retries < max_retries:
                    # Throttling, exponential backoff
                    sleep_time = min(max_backoff, backoff_base ** retries + random.uniform(0, 1))
                    time.sleep(sleep_time)
                    retries += 1
                else:
                    raise e
            else:
                # Some other API error, rethrow
                raise
                
def query_llm(prompts,params):
    import json
    import boto3   
    if "claude" in params["sql_model"].lower():
        answer, input_token, output_token=_invoke_bedrock_with_retries([], params, prompts)
        tokens=input_token+output_token
        pricing=input_token*pricing_file[params['sql_model']]["input"]+output_token*pricing_file[params['sql_model']]["output"]
        st.session_state['output_token']+=output_token
        st.session_state['input_token']+=input_token
        st.session_state['cost']+=pricing
        return answer
    elif "mixtral" in params["sql_model"].lower():
       
        payload = {
            "inputs":prompts,
            "parameters": {"max_new_tokens": params['sql_token'],
                           # "top_p": params['top_p'], 
                           "temperature": 0.1,
                           "return_full_text": False,}
        }
        llama=boto3.client("sagemaker-runtime")
        output=llama.invoke_endpoint(Body=json.dumps(payload), EndpointName=MIXTRAL_ENPOINT,ContentType="application/json")
        answer=json.loads(output['Body'].read().decode())[0]['generated_text']
        tkn=mistral_counter("mistralai/Mistral-7B-v0.1")
        input_token=len(tkn.encode(prompts))
        output_token=len(tkn.encode(answer))       
        tokens=input_token+output_token     
        st.session_state['output_token']+=output_token
        st.session_state['input_token']+=input_token 
        return answer


def summary_llm(prompts,params, handler=None):
    import json    
    if 'claude' in params['model_id'].lower():
        answer, input_token, output_token=_invoke_bedrock_with_retries([], params, prompts, handler)  
        tokens=input_token+output_token
        pricing=input_token*pricing_file[params['sql_model']]["input"]+output_token*pricing_file[params['sql_model']]["output"]
        st.session_state['output_token']+=output_token
        st.session_state['input_token']+=input_token
        st.session_state['cost']+=pricing
    return answer

def chunk_csv_rows(csv_rows, max_token_per_chunk=10000):
    header = csv_rows[0]  # Assuming the first row is the header
    csv_rows = csv_rows[1:]  # Remove the header from the list
    current_chunk = []
    current_token_count = 0
    chunks = []
    header_token=CLAUDE.count_tokens(header)
    for row in csv_rows:
        token = CLAUDE.count_tokens(row)  # Assuming that the row is a space-separated CSV row.
        # print(token)
        if current_token_count + token+header_token <= max_token_per_chunk:
            current_chunk.append(row)
            current_token_count += token
        else:
            if not current_chunk:
                raise ValueError("A single CSV row exceeds the specified max_token_per_chunk.")
            header_and_chunk=[header]+current_chunk
            chunks.append("\n".join([x for x in header_and_chunk]))
            current_chunk = [row]
            current_token_count = token

    if current_chunk:
        last_chunk_and_header=[header]+current_chunk
        chunks.append("\n".join([x for x in last_chunk_and_header]))

    return chunks

def llm_memory(question, params=None):
    """ This function determines the context of each new question looking at the conversation history...
        to send the appropiate question to the retriever.    
        Messages are stored in memory.
    """
    chat_string = ""
    for entry in st.session_state['messages'][-5:]:
        if "role" in entry:
            role = entry["role"]
            content = entry["content"]
            chat_string += f"{role}: {content}\n"
    
    memory_template = f"""Here is the history of a conversation between a user and an assistant. The last conversation being the most recent:
<conversation>
{chat_string}
</conversation>

Your job is to determine if the question is context-dependent to the most recent conversation:
- If it is, rephrase the question as an independent question in its entirety so that someone who has no prior context is able to understand the question. Keep it as natural language, DO NOT rephrase to a SQL query.
- If it is not, respond with "no".

Here is a new question from the user:
user: {question}

Remember, your job is NOT to answer the user question!
Format your response as:
<response>
answer
</response>
Do not provide any preamble in your answer, just provide the rephrased question or "no" given the instruction above."""
    if chat_string:        
        
        content={"role": "user",
                "content": [{
            "type": "text",
            "text":memory_template
                }]}
        prompt = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 100,
            "temperature": 0.1,
            "system":"",
            "messages": [content]
        }
        prompt=json.dumps(prompt)
        output = BEDROCK.invoke_model(body=prompt, modelId='anthropic.claude-3-sonnet-20240229-v1:0', accept="application/json",  contentType="application/json")   
        response_body = json.loads(output.get('body').read())
        answer=response_body.get('content')[0]['text']
        idx1 = answer.index('<response>')
        idx2 = answer.index('</response>')
        question_2=answer[idx1 + len('<response>') + 1: idx2]
        if 'no' != question_2.strip():
            question=question_2
        # print(question)
        input_token=CLAUDE.count_tokens(memory_template)
        output_token=CLAUDE.count_tokens(question)
        tokens=input_token+output_token
        st.session_state['output_token']+=output_token
        st.session_state['input_token']+=input_token
        pricing=input_token*pricing_file['anthropic.claude-3-sonnet-20240229-v1:0']["input"]+output_token*pricing_file['anthropic.claude-v2']["output"]
    return question

                    
def db_chatter(params):   
    import io
    st.title('Converse with Redshift')
    for message in st.session_state.messages:
        if "role" in message.keys():
            with st.chat_message(message["role"]):  
                st.markdown(message["content"].replace("$","USD ").replace("%", " percent"))
        else:
            with st.expander(label="**Metadata**"):              
                st.dataframe(message["df"])           
                st.code(message["sql"])
                st.markdown(f"* **Elapsed Timed**: {round(float(message['time']))}s")                  
            
    if prompt := st.chat_input("Hello?"):
        if params["memory"]:
            prompt=llm_memory(prompt, params=None) 
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
            
            
        with st.chat_message("assistant"):
            time_now=time.time()
            message_placeholder = st.empty()
            params['prompt']=prompt       
            answer, sql_state, data=redshift_qna(params,message_placeholder)
            time_diff=time.time()-time_now
            if data:
                data = io.StringIO(data)
                df=pd.read_csv(data) 
            else:
                df=pd.DataFrame()
            message_placeholder.markdown(answer.replace("$","USD ").replace("%", " percent"))
            st.session_state.messages.append({"role": "assistant", "content": answer})       
            with st.expander(label="**Metadata**"): 
                st.dataframe(df)
                st.code(sql_state)
                st.markdown(f"* **Elapsed Timed**: {time_diff}")           
                st.session_state.messages.append({"time": time_diff,"df": df, "sql":sql_state})
        st.rerun()
          
        
def app_sidebar():
    with st.sidebar:
        st.metric(label="Bedrock Session Cost", value=f"${round(st.session_state['cost'],2)}")  
        st.write('### User Preference')
        temp = st.slider('Temperature', min_value=0., max_value=1., value=0.0, step=0.01)
        mem = st.checkbox('chat memory')
        query_type=st.selectbox('Query Type', ["Table Chat","DataBase Chat"])  
        models=["anthropic.claude-3-sonnet-20240229-v1:0",                
                 'anthropic.claude-v2:1',
                 'anthropic.claude-v2']
        model=st.selectbox('Text Model', models)  
        sql_models=["anthropic.claude-3-sonnet-20240229-v1:0",
            'anthropic.claude-v2',              
                    'anthropic.claude-v2:1',
                   ]
        sql_model=st.selectbox('SQL Model', sql_models)         
        sql_token_length = st.slider('SQL Token Length', min_value=50, max_value=2000, value=500, step=10)
        qna_length = st.slider('Text Token Length', min_value=50, max_value=2000, value=550, step=10)       
        db=get_db_redshift(CLUSTER_IDENTIFIER, DATABASE, SERVERLESS,DB_USER)
        database=st.selectbox('Select Database',options=db,index=1)   
        schm=get_schema_redshift(CLUSTER_IDENTIFIER, database,SERVERLESS, DB_USER)
        schema=st.selectbox('Select SchemaName',options=schm)#,index=6)
        tab=get_tables_redshift(CLUSTER_IDENTIFIER, database, schema,SERVERLESS,DB_USER,)
        tab=[x for x in tab if x!="holdings_pkey"]
        engine="redshift" 
        if "table" in query_type.lower():
            tab=get_tables_redshift(CLUSTER_IDENTIFIER, database, schema,SERVERLESS,DB_USER,)   
            tab=[x for x in tab if x!="holdings_pkey"]
            tables=st.selectbox('Select Tables',options=tab)     
            params={'sql_token':sql_token_length,'qna_token':qna_length,'table':tables,'db':database,"db_schema":schema,'temp':temp,'model_id':model, 
                    'sql_model':sql_model, "memory":mem,"engine":engine,"serverless":SERVERLESS,"chat_history":[],"system_message":""} 
        elif "database" in query_type.lower():
            params={'sql_token':sql_token_length,'qna_token':qna_length,'db':database,'tables':tab,"db_schema":schema,'temp':temp,'model_id':model, 
                    'sql_model':sql_model, "memory":mem,"engine":engine, "serverless":SERVERLESS,"chat_history":[],"system_message":""} 
        return params

      
        
def main():
    params=app_sidebar()
    db_chatter(params)  

if __name__ == '__main__':
    main()       
