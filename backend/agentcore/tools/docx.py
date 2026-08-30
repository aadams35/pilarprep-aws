from __future__ import annotations

from io import BytesIO
from typing import Any, Iterable, Mapping
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


def _paragraph(
    text: object,
    style: str | None = None,
    number_id: int | None = None,
) -> str:
    properties: list[str] = []
    if style:
        properties.append(f'<w:pStyle w:val="{style}"/>')
    if number_id is not None:
        properties.append(
            f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{number_id}"/></w:numPr>'
        )
    property_xml = f"<w:pPr>{''.join(properties)}</w:pPr>" if properties else ""
    safe_text = escape(str(text or ""))
    return (
        f"<w:p>{property_xml}<w:r><w:t xml:space=\"preserve\">"
        f"{safe_text}</w:t></w:r></w:p>"
    )


def _register_lines(items: object) -> Iterable[str]:
    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            title = str(item.get("title") or "Item")
            detail = str(item.get("detail") or "")
            owner = str(item.get("owner") or "Owner TBD")
            status = str(item.get("status") or "Open")
            lines.append(f"{title}: {detail} Owner: {owner}. Status: {status}.")
    return lines


def _numbering_xml(count: int = 8) -> str:
    instances = "".join(
        f'<w:num w:numId="{number_id}"><w:abstractNumId w:val="0"/></w:num>'
        for number_id in range(1, count + 1)
    )
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="0">
    <w:multiLevelType w:val="singleLevel"/>
    <w:lvl w:ilvl="0">
      <w:start w:val="1"/>
      <w:numFmt w:val="decimal"/>
      <w:lvlText w:val="%1."/>
      <w:lvlJc w:val="left"/>
      <w:pPr><w:tabs><w:tab w:val="num" w:pos="420"/></w:tabs><w:ind w:left="420" w:hanging="240"/></w:pPr>
    </w:lvl>
  </w:abstractNum>
  {instances}
</w:numbering>'''


def handoff_docx_bytes(packet: Mapping[str, Any], scope: Mapping[str, str]) -> bytes:
    company = str(packet.get("company") or scope["clientId"])
    citations = [
        str(source)
        for source in packet.get("citations", [])
        if isinstance(source, str) and source.strip()
    ]
    sections = [
        _paragraph(f"PilarPrep Project Handoff | {company}", "Title"),
        _paragraph("Approved meeting context translated into delivery continuity", "Subtitle"),
        _paragraph(
            f"Tenant {scope['tenantId']} | Client {scope['clientId']} | Project {scope['projectId']}",
            "Meta",
        ),
        _paragraph("Handoff Summary", "Heading1"),
        _paragraph(packet.get("projectAnswer") or "No summary was generated."),
    ]
    if citations:
        sections.append(_paragraph(f"Grounded by: {' | '.join(citations)}", "SourceNote"))
    business_case = packet.get("businessCase")
    if isinstance(business_case, Mapping):
        sections.append(_paragraph("Business Case", "Heading1"))
        for key, label in (
            ("scenario", "Business Scenario"),
            ("desiredOutcomes", "Desired Outcomes"),
            ("alignmentStatement", "Meeting Alignment Statement"),
            ("inScope", "What We Will Cover"),
            ("outOfScope", "What We Will Not Cover"),
            ("successCriteria", "Success Criteria"),
        ):
            sections.append(_paragraph(label, "Heading1"))
            sections.append(_paragraph(business_case.get(key, "")))



    artifacts = packet.get("projectArtifacts")
    if isinstance(artifacts, Mapping):
        for number_id, (heading, key) in enumerate(
            (
                ("Two-Week Plan", "twoWeekPlan"),
                ("Risk Register", "riskRegister"),
                ("Stakeholder Map", "stakeholderMap"),
            ),
            start=1,
        ):
            sections.append(_paragraph(heading, "Heading1"))
            rows = list(_register_lines(artifacts.get(key)))
            sections.extend(
                _paragraph(row, "ListParagraph", number_id)
                for row in (rows or ["No items generated."])
            )

        follow_up = artifacts.get("followUpEmail")
        if isinstance(follow_up, Mapping):
            sections.append(_paragraph("Follow-Up Email", "Heading1"))
            sections.append(_paragraph(f"Subject: {follow_up.get('subject', '')}", "Meta"))
            sections.append(_paragraph(follow_up.get("body", "")))

        next_steps = artifacts.get("nextSteps")
        if isinstance(next_steps, Mapping):
            sections.append(_paragraph("Next Steps", "Heading1"))
            actions = next_steps.get("immediateActions")
            if isinstance(actions, list):
                for action in actions:
                    if not isinstance(action, Mapping):
                        continue
                    sections.append(
                        _paragraph(
                            f"{action.get('action', '')} | Owner: {action.get('owner', '')} | "
                            f"Timing: {action.get('timing', '')} | Dependency: {action.get('dependency', '')} | "
                            f"Decision gate: {action.get('decisionGate', '')}",
                            "ListParagraph",
                            4,
                        )
                    )

            sections.append(_paragraph("Open Questions", "Heading1"))
            questions = next_steps.get("openQuestions")
            if isinstance(questions, list):
                sections.extend(
                    _paragraph(question, "ListParagraph", 5)
                    for question in questions
                    if isinstance(question, str) and question.strip()
                )

            meeting = next_steps.get("nextMeeting")
            if isinstance(meeting, Mapping):
                attendees = meeting.get("attendees")
                attendee_text = ", ".join(str(item) for item in attendees) if isinstance(attendees, list) else ""
                sections.append(_paragraph("Next Meeting", "Heading1"))
                sections.append(
                    _paragraph(
                        f"{meeting.get('purpose', '')} | {meeting.get('timing', '')} | Attendees: {attendee_text}"
                    )
                )
            sections.append(_paragraph("Customer-Facing Summary", "Heading1"))
            sections.append(_paragraph(next_steps.get("customerSummary", "")))
            sections.append(_paragraph("Internal Notes", "Heading1"))
            sections.append(_paragraph(next_steps.get("internalNotes", "")))

    if citations:
        sections.append(_paragraph("Approved Source Labels", "Heading1"))
        sections.extend(
            _paragraph(source, "ListParagraph", 4)
            for source in citations
        )

    body_xml = "".join(sections)
    safe_company = escape(company)
    document_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>{body_xml}<w:sectPr><w:footerReference w:type="default" r:id="rId3"/><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1260" w:left="1440" w:footer="720"/></w:sectPr></w:body>
</w:document>'''
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:color w:val="172235"/><w:sz w:val="21"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="150" w:line="300" w:lineRule="auto"/><w:widowControl/></w:pPr></w:pPrDefault></w:docDefaults>
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="150" w:line="300" w:lineRule="auto"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Subtitle"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:after="80"/></w:pPr><w:rPr><w:b/><w:color w:val="0F6B93"/><w:sz w:val="40"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/><w:next w:val="Meta"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:after="80"/></w:pPr><w:rPr><w:color w:val="446076"/><w:sz w:val="24"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Meta"><w:name w:val="Meta"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="100"/></w:pPr><w:rPr><w:color w:val="667789"/><w:sz w:val="18"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="320" w:after="110"/><w:pBdr><w:bottom w:val="single" w:sz="6" w:space="5" w:color="B7CAD7"/></w:pBdr></w:pPr><w:rPr><w:b/><w:color w:val="172235"/><w:sz w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListParagraph"><w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="120"/></w:pPr></w:style>
  <w:style w:type="paragraph" w:styleId="SourceNote"><w:name w:val="Source Note"/><w:basedOn w:val="Normal"/><w:qFormat/><w:pPr><w:ind w:left="420"/><w:spacing w:after="150"/></w:pPr><w:rPr><w:i/><w:color w:val="526070"/><w:sz w:val="17"/></w:rPr></w:style>
</w:styles>'''
    numbering_xml = _numbering_xml()
    footer_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:color w:val="667789"/><w:sz w:val="16"/></w:rPr><w:t>PilarPrep | {safe_company} | </w:t></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>PAGE</w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>
</w:ftr>'''
    content_types_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
  <Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
</Types>'''
    rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    document_rels_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>
</Relationships>'''

    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as docx:
        docx.writestr("[Content_Types].xml", content_types_xml)
        docx.writestr("_rels/.rels", rels_xml)
        docx.writestr("word/document.xml", document_xml)
        docx.writestr("word/_rels/document.xml.rels", document_rels_xml)
        docx.writestr("word/styles.xml", styles_xml)
        docx.writestr("word/numbering.xml", numbering_xml)
        docx.writestr("word/footer1.xml", footer_xml)
    return output.getvalue()