def parse(raw: str) -> dict:
    lines = raw.split('\n')
    title = ''
    body_lines = []
    title_found = False
    for line in lines:
        if not title_found and line.startswith('# '):
            title = line[2:]
            title_found = True
        else:
            body_lines.append(line)
    return {'title': title, 'body': '\n'.join(body_lines)}
