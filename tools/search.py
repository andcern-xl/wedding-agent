import os
import httpx


def web_search(query: str, num_results: int = 5) -> list[dict]:
    """Search the web using Tavily. Returns list of {title, url, content}."""
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key:
        return [{"error": "TAVILY_API_KEY not set"}]

    try:
        response = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "num_results": num_results,
                "search_depth": "basic",
                "include_answer": True,
            },
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        results = []
        if data.get("answer"):
            results.append({"title": "Summary", "url": "", "content": data["answer"]})
        for r in data.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", "")[:500],
            })
        return results
    except Exception as e:
        return [{"error": str(e)}]
