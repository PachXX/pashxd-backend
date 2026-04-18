import textstat
import re


def get_readability(text: str) -> dict:
    # Strip HTML tags if any
    clean = re.sub(r"<[^>]+>", "", text)

    flesch = round(textstat.flesch_reading_ease(clean), 1)

    # Grade label
    if flesch >= 80:
        grade = "Easy"
    elif flesch >= 60:
        grade = "Standard"
    elif flesch >= 40:
        grade = "Difficult"
    else:
        grade = "Very Difficult"

    # SEO score (basic heuristics)
    word_count = len(clean.split())
    seo_score = 100
    tips = []

    if word_count < 300:
        seo_score -= 20
        tips.append("Content is too short. Aim for at least 300 words.")
    if word_count > 2500:
        tips.append("Long content — great for SEO!")

    return {
        "flesch_reading_ease": flesch,
        "reading_grade": grade,
        "reading_time_minutes": max(1, round(word_count / 200)),
        "word_count": word_count,
        "sentence_count": textstat.sentence_count(clean),
        "seo_score": max(0, seo_score),
        "tips": tips,
    }


def get_keyword_density(text: str, top_n: int = 10) -> list:
    clean = re.sub(r"<[^>]+>", "", text).lower()
    # Remove common stop words
    stop_words = {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to",
        "for", "of", "with", "by", "from", "is", "was", "are", "were",
        "be", "been", "being", "have", "has", "had", "do", "does", "did",
        "will", "would", "could", "should", "may", "might", "this", "that",
        "these", "those", "it", "its", "we", "you", "he", "she", "they",
    }
    words = re.findall(r'\b[a-z]{3,}\b', clean)
    filtered = [w for w in words if w not in stop_words]
    total = len(filtered)

    freq = {}
    for word in filtered:
        freq[word] = freq.get(word, 0) + 1

    sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:top_n]

    return [
        {
            "keyword": word,
            "count": count,
            "density": round((count / total) * 100, 2) if total > 0 else 0,
        }
        for word, count in sorted_words
    ]