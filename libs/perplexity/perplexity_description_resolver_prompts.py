def prompt_generate_repo_description_system() -> str:
    return """Summarize the following GitHub project using the readme in less than 300 words. Make sure to include what the project is about, what it does, and how to use it.
    """


# noinspection PyUnusedLocal
def prompt_generate_repo_description_user(
    url: str,
) -> str:
    return f"""
        Summarize the following GitHub repository page in less than 300 words:
        - **Repository URL:** `{url}`
    """
