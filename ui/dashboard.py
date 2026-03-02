"""
Open Brain Dashboard.
Streamlit web interface for memory management.
"""
import os
import sys
import streamlit as st
import pandas as pd

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import init_db
from db.queries import (
    search_memories,
    get_memory_stats,
    get_recent_memories,
    insert_memory
)
from embedder import create_embedding
from extractors.entities import extract_entities
from extractors.tagger import auto_tag


# Page config
st.set_page_config(
    page_title="Open Brain",
    page_icon="🧠",
    layout="wide"
)


def init_session():
    """Initialize session state."""
    if 'initialized' not in st.session_state:
        try:
            init_db()
        except Exception as e:
            st.error(f"Database connection failed: {e}")
        st.session_state.initialized = True


def search_memories_ui():
    """Memory search interface."""
    st.header("🔍 Search Memories")
    
    col1, col2 = st.columns([3, 1])
    
    with col1:
        query = st.text_input("Search query", placeholder="What are you looking for?")
    
    with col2:
        limit = st.slider("Results", 5, 50, 10)
    
    if query:
        with st.spinner("Searching..."):
            try:
                embedding = create_embedding(query)
            except Exception as e:
                st.warning(f"Could not create embedding: {e}")
                embedding = None
            
            results = search_memories(
                query=query,
                embedding=embedding,
                limit=limit
            )
        
        st.write(f"Found {len(results)} results:")
        
        for mem in results:
            with st.expander(f"{mem.get('content', '')[:80]}..."):
                st.write(mem.get('content'))
                st.caption(f"Source: {mem.get('source')} | Tags: {', '.join(mem.get('tags', []))}")
                st.caption(f"Created: {mem.get('created_at')}")
    else:
        # Show recent memories
        recent = get_recent_memories(limit=10)
        st.write("Recent memories:")
        
        for mem in recent:
            with st.expander(f"{mem.get('content', '')[:80]}..."):
                st.write(mem.get('content'))
                st.caption(f"Source: {mem.get('source')} | Tags: {', '.join(mem.get('tags', []))}")


def create_memory_ui():
    """Memory creation interface."""
    st.header("➕ Create Memory")
    
    content = st.text_area("Content", height=150, placeholder="What's on your mind?")
    
    col1, col2 = st.columns(2)
    
    with col1:
        source = st.selectbox("Source", ["chat", "note", "email", "cli", "dashboard"])
    
    with col2:
        importance = st.slider("Importance", 0.0, 1.0, 0.5)
    
    tags = st.text_input("Tags (comma separated)", placeholder="work, idea, important")
    
    if st.button("Store Memory"):
        if content:
            # Parse tags
            tag_list = [t.strip() for t in tags.split(',') if t.strip()]
            
            # Extract entities and auto-tag
            entities = extract_entities(content)
            auto_tags = auto_tag(content, entities, source, tag_list)
            
            # Generate embedding
            try:
                embedding = create_embedding(content)
            except Exception as e:
                st.warning(f"Could not create embedding: {e}")
                embedding = None
            
            # Store
            memory_id = insert_memory(
                source=source,
                content=content,
                embedding=embedding,
                entities=entities,
                tags=list(auto_tags.keys()),
                tag_sources=auto_tags,
                importance=importance,
                metadata={}
            )
            
            st.success(f"Memory stored! ID: {memory_id}")
        else:
            st.warning("Please enter some content")


def stats_ui():
    """Statistics interface."""
    st.header("📊 Statistics")
    
    with st.spinner("Loading stats..."):
        stats = get_memory_stats()
    
    # Overview metrics
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Total Memories", stats.get('total', 0))
    
    with col2:
        by_source = stats.get('by_source', {})
        st.metric("Sources", len(by_source))
    
    with col3:
        top_tags = stats.get('top_tags', [])
        st.metric("Unique Tags", len(top_tags))
    
    with col4:
        weekly = stats.get('weekly_activity', [])
        this_week = sum(d.get('count', 0) for d in weekly)
        st.metric("This Week", this_week)
    
    # By source chart
    if by_source:
        st.subheader("By Source")
        df_source = pd.DataFrame([
            {"Source": k, "Count": v} for k, v in by_source.items()
        ])
        st.bar_chart(df_source.set_index("Source"))
    
    # Top tags
    if top_tags:
        st.subheader("Top Tags")
        df_tags = pd.DataFrame(top_tags[:15], columns=["Tag", "Count"])
        st.bar_chart(df_tags.set_index("Tag"))
    
    # Weekly activity
    if weekly:
        st.subheader("Weekly Activity")
        df_weekly = pd.DataFrame(weekly)
        st.line_chart(df_weekly.set_index("date"))


def trends_ui():
    """Trends interface."""
    st.header("📈 Trends")
    
    from analytics.trends import TrendAnalyzer
    
    analyzer = TrendAnalyzer()
    
    weeks = st.slider("Weeks to analyze", 1, 12, 4)
    
    with st.spinner("Analyzing trends..."):
        trends = analyzer.get_trending_topics(weeks)
    
    if trends:
        st.subheader("Trending Topics")
        
        for topic in trends:
            col1, col2 = st.columns([3, 1])
            with col1:
                st.write(f"**{topic['name']}** ({topic['type']})")
            with col2:
                st.write(f"↑ {topic['change']}%")
            
            st.progress(min(topic['change'] / 100, 1.0))
    else:
        st.info("No trends data available yet. Add more memories to see trends.")


def main():
    """Main dashboard."""
    st.title("🧠 Open Brain")
    st.caption("Your personal memory management system")
    
    init_session()
    
    # Sidebar navigation
    page = st.sidebar.radio(
        "Navigation",
        ["Search", "Create", "Statistics", "Trends"]
    )
    
    if page == "Search":
        search_memories_ui()
    elif page == "Create":
        create_memory_ui()
    elif page == "Statistics":
        stats_ui()
    elif page == "Trends":
        trends_ui()


if __name__ == "__main__":
    main()
