import pandas as pd
import numpy as np
import faiss
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

df = pd.read_csv("notion_knowledge.csv")
model = SentenceTransformer("all-MiniLM-L6-v2")
embeddings = model.encode(df["content"].tolist(), convert_to_tensor=False)
df["embedding"] = embeddings.tolist()

def query_with_context(query, top_k=3):
    q_vec = model.encode([query])[0]
    sims = cosine_similarity([q_vec], embeddings)[0]
    top = sims.argsort()[-top_k:][::-1]
    context = "\n---\n".join(df.iloc[top]["content"].tolist())
    return context
