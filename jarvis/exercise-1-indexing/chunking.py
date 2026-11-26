"""The two core parts of the Indexing exercise.

This file only defines functions; build_index.py calls them.
The documents are already loaded by the provided code. Your job is to choose how to
split them into chunks, and to keep enough information with each chunk to trace it
back to its source and identify the source's version.
"""
import re  # used to find paragraph boundaries


def split_document(document, max_chars=900):  # split a document into chunks with offsets into the source
    """Take a document and return a list of chunks, following the contract in the README.

    document['body'] is the original text without the title and metadata block.
    Each result: {'text': text, 'start': offset from the start of body, 'end': end offset}.
    end is the position after the last character, so body[start:end] must equal text.
    Overlap is allowed, but the text must be fully covered, in source order, with bounded chunk sizes.
    """
    body = document["body"]  # work only with offsets into the original text; never modify the document
    spans = []  # (start, end) units, each no longer than max_chars
    for match in re.finditer(r"\S(?:.*?\S)??(?=\n[ \t]*\n|\s*\Z)", body, re.S):  # a paragraph runs until a blank line
        spans.extend(_split_long(body, match.start(), match.end(), max_chars))  # long paragraphs are split by lines, then by spaces
    pieces = []  # the final chunks
    current_start, current_end = None, None  # the chunk currently being built by merging adjacent paragraphs
    for start, end in spans:  # merge short paragraphs while the chunk stays within the size limit
        if current_start is not None and end - current_start <= max_chars:  # the merged chunk also includes the blank line between paragraphs
            current_end = end  # extend the current chunk to the end of this paragraph
            continue
        if current_start is not None:  # the paragraph doesn't fit: close the previous chunk
            pieces.append({"text": body[current_start:current_end], "start": current_start, "end": current_end})  # text always equals body[start:end]
        current_start, current_end = start, end  # start a new chunk from the current paragraph
    if current_start is not None:  # keep the last chunk
        pieces.append({"text": body[current_start:current_end], "start": current_start, "end": current_end})
    return pieces  # chunks cover all the text, in source order, with bounded size


def _split_long(body, start, end, max_chars):  # split a long span into parts no longer than max_chars
    parts = []
    while end - start > max_chars:  # while what remains is still too long
        window = body[start:start + max_chars]  # the largest part we may take
        cut = window.rfind("\n")  # prefer cutting at a line end, so a table row or condition stays whole
        if cut <= 0:  # no usable line end
            cut = window.rfind(" ")  # cut at the last space so no word is broken
        if cut <= 0:  # one very long word with no spaces
            cut = max_chars  # a hard cut is the last resort
        piece_end = start + len(window[:cut].rstrip())  # don't include trailing whitespace in the part
        parts.append((start, piece_end))
        start += cut  # continue from the cut point
        while start < end and body[start].isspace():  # skip whitespace before the next part
            start += 1  # the next part starts at a non-space character
    if start < end:  # the remainder is short enough
        parts.append((start, end))
    return parts  # parts in source order


def make_metadata(document, piece):  # the source fields stored with every chunk
    """Return metadata for one chunk, without changing the document or the chunk.

    Return the fields: document_id, version, status, updated, source, title.
    The values are in the document dict. source is the relative path to the source file.
    """
    fields = ("document_id", "version", "status", "updated", "source", "title")  # source identity, version, validity and a path to open
    metadata = {field: document[field] for field in fields}  # a new dict: the document and chunk are left unchanged
    for field in ("path", "meeting_date", "project_id"):  # Obsidian vault notes: path in the vault, meeting date, project
        if document.get(field):
            metadata[field] = document[field]
    return metadata


# ---------------------------------------------------------------------------
# Bonus 1 - structure-aware chunking
# ---------------------------------------------------------------------------
# Each chunk has two representations:
#   text / source_text  - the exact span from the source: body[start:end] (traceable).
#   embedding_text      - what is sent to the model: section heading and table header + text.
# A section heading stays with the content that follows it, and a long table is split only between rows.

PARAGRAPH = re.compile(r"\S(?:.*?\S)??(?=\n[ \t]*\n|\s*\Z)", re.S)  # a paragraph runs until a blank line


def split_document_structured(document, max_chars=900):  # split by sections and tables, with separate context for the model
    body = document["body"]  # the original text; never modified
    units = []  # units that must not be broken while merging: (start, end, section, table_header or None)
    section, heading_start = None, None  # the current section, and the start of a heading still waiting for its content
    for match in PARAGRAPH.finditer(body):  # walk the paragraphs in order
        start, end = match.start(), match.end()
        text = body[start:end]
        if text.startswith("#") and "\n" not in text:  # a heading line on its own
            if heading_start is not None:  # a previous heading with no content becomes its own unit
                units.append((heading_start, _heading_end(body, heading_start), section, None))  # never drop a heading
            section, heading_start = text.lstrip("#").strip(), start  # attach this heading to the next unit
            continue
        first = heading_start if heading_start is not None else start  # the unit starts at the heading, if there is one
        heading_start = None  # the heading has been attached
        lines = text.split("\n")
        if len(lines) > 2 and all(line.lstrip().startswith("|") for line in lines):  # a Markdown table: every line starts with |
            units.extend(_split_table(body, first, start, end, max_chars, section))  # split between rows and attach column headers
        else:  # an ordinary paragraph
            for part_start, part_end in _split_long(body, first, end, max_chars):  # long paragraphs are split as in the core version
                units.append((part_start, part_end, section, None))  # no table header needed
    if heading_start is not None:  # a final heading with no content
        units.append((heading_start, _heading_end(body, heading_start), section, None))  # kept so no text is lost
    pieces = []  # the final chunks
    for start, end, unit_section, table_header in units:  # merge adjacent units while the size allows
        can_merge = pieces and table_header is None and end - pieces[-1]["start"] <= max_chars  # a table continuation always starts a new chunk
        if can_merge:  # the unit fits into the previous chunk
            last = pieces[-1]
            last["end"] = end
            last["text"] = last["source_text"] = body[last["start"]:end]  # text stays an exact span from the source
        else:  # start a new chunk
            pieces.append({"text": body[start:end], "source_text": body[start:end], "start": start, "end": end,
                           "_section": unit_section, "_table_header": table_header})  # context added only to embedding_text
    for piece in pieces:  # build the text sent to the model
        context = []  # context lines that are not part of the source span
        if piece["_section"] and not piece["text"].startswith("#"):  # the chunk starts in the middle of a section
            context.append("## " + piece["_section"])  # add the section heading
        if piece["_table_header"]:  # the chunk continues a table
            context.append(piece["_table_header"])  # add the column headers
        piece["embedding_text"] = "\n".join(context + [piece["text"]])  # context + the original text
        del piece["_section"], piece["_table_header"]  # internal helper fields are not saved
    return pieces  # each chunk: text, source_text, start, end, embedding_text


def _heading_end(body, start):  # end of the heading line that starts at start
    end = body.find("\n", start)  # a heading is a single line
    return len(body) if end == -1 else end  # heading at the very end of the document


def _split_table(body, first, table_start, table_end, max_chars, section):  # split a table between rows only
    rows = []  # (start, end) of each table row
    position = table_start  # start at the table's header row
    for line in body[table_start:table_end].split("\n"):
        rows.append((position, position + len(line.rstrip())))  # exact offsets of the row
        position += len(line) + 1  # +1 for the newline
    header = body[rows[0][0]:rows[1][1]]  # header row + separator row
    units, part_start, part_end, is_first = [], first, rows[1][1], True  # the first part includes the section heading and column headers
    for row_start, row_end in rows[2:]:  # the data rows
        if row_end - part_start <= max_chars:  # the row fits into the current part
            part_end = row_end
            continue
        if part_end - part_start > max_chars:  # the current part alone is too long (e.g. heading + one huge row)
            raise ValueError("Table row longer than max_chars; increase --max-chars")  # never cut a table row in half
        units.append((part_start, part_end, section, None if is_first else header))  # the first part already contains the headers
        part_start, part_end, is_first = row_start, row_end, False  # a new part starts at the current row
    if part_end - part_start > max_chars:  # same check for the last part
        raise ValueError("Table row longer than max_chars; increase --max-chars")
    units.append((part_start, part_end, section, None if is_first else header))  # keep the last part
    return units  # table parts in source order
