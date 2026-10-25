"""Supported article languages and deterministic reader-facing labels.

These profiles select an output language; they do not certify every routed
model's fluency. The application interface and operational messages stay English.
"""

import unicodedata
from dataclasses import dataclass
from typing import Literal

ArticleLanguage = Literal["en", "tr", "es", "pt", "de"]


def fold_for_matching(value: str) -> str:
    """Compare Unicode evidence without dropping accents or dotted-I words."""
    # Case folding capital İ produces i + a combining dot, which Python's
    # word-character class otherwise splits in the middle of a Turkish word.
    return unicodedata.normalize("NFC", value).casefold().replace("i\u0307", "i")


@dataclass(frozen=True, slots=True)
class LanguageProfile:
    name: str
    native_name: str
    references: str
    retrieved: str
    source: str
    synthetic_notice: str
    unresearched_notice: str
    fixture_sections: tuple[str, str]
    fixture_prefix: str
    fixture_sentence: str


LANGUAGES: dict[str, LanguageProfile] = {
    "en": LanguageProfile(
        "English", "English", "References", "retrieved", "Source",
        "Synthetic development draft. No live model or web research was used.",
        "Unresearched draft. Review factual claims before publication.",
        ("A starting point", "What to consider next"),
        "Synthetic development example for {topic}. ",
        "This paragraph demonstrates the writing workflow and needs human editing before publication. ",
    ),
    "tr": LanguageProfile(
        "Turkish", "Türkçe", "Kaynaklar", "erişim tarihi", "Kaynak",
        "Yapay geliştirme taslağı. Gerçek bir model veya web araştırması kullanılmadı.",
        "Araştırma yapılmadan oluşturulan taslak. Yayımlamadan önce olgusal iddiaları inceleyin.",
        ("Bir başlangıç noktası", "Sonraki adımlar"),
        "{topic} konusu için yapay geliştirme örneği. ",
        "Bu paragraf yazım sürecini gösterir ve yayımlanmadan önce bir kişi tarafından düzenlenmelidir. ",
    ),
    "es": LanguageProfile(
        "Spanish", "Español", "Referencias", "consultado", "Fuente",
        "Borrador de prueba sintético. No se utilizó un modelo real ni investigación web.",
        "Borrador sin investigación. Revise las afirmaciones antes de publicarlo.",
        ("Un punto de partida", "Próximos pasos"),
        "Ejemplo de desarrollo sintético sobre {topic}. ",
        "Este párrafo demuestra el proceso de escritura y necesita revisión humana antes de su publicación. ",
    ),
    "pt": LanguageProfile(
        "Portuguese", "Português", "Referências", "consultado em", "Fonte",
        "Rascunho de teste sintético. Não foi utilizado um modelo real nem pesquisa na web.",
        "Rascunho sem pesquisa. Reveja as afirmações factuais antes da publicação.",
        ("Um ponto de partida", "Próximos passos"),
        "Exemplo de desenvolvimento sintético sobre {topic}. ",
        "Este parágrafo demonstra o processo de escrita e precisa de revisão humana antes da publicação. ",
    ),
    "de": LanguageProfile(
        "German", "Deutsch", "Quellen", "abgerufen am", "Quelle",
        "Synthetischer Testentwurf. Es wurden weder ein echtes Modell noch eine Webrecherche verwendet.",
        "Entwurf ohne Recherche. Prüfen Sie die Tatsachenbehauptungen vor der Veröffentlichung.",
        ("Ein Ausgangspunkt", "Die nächsten Schritte"),
        "Synthetisches Entwicklungsbeispiel zum Thema {topic}. ",
        "Dieser Absatz veranschaulicht den Schreibablauf und muss vor der Veröffentlichung von einem Menschen überarbeitet werden. ",
    ),
}


def language_profile(language: str) -> LanguageProfile:
    try:
        return LANGUAGES[language]
    except KeyError:
        raise ValueError("Choose English, Turkish, Spanish, Portuguese or German.") from None


def writing_instruction(language: str) -> str:
    profile = language_profile(language)
    return (
        f" The article output language is {profile.name} ({profile.native_name}; {language})."
        " Write the article title, section headings, prose and rewritten sections in this language,"
        " even when the brief or sources use another language. Preserve proper names, source IDs,"
        " citation markers and schema keys. Translate source meaning faithfully; never invent support."
        " Review idiomatic wording, grammar and language consistency as well as factual support."
        " Report a language_mismatch issue if the prose uses the wrong language."
        " Keep operational review explanations and issue codes in English."
    )
