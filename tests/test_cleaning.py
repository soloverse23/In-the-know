"""Cleaning from chat: the exact problems found in a real test, fixed with the editing tools, all undoable."""
import asyncio

from intheknow.context import ContextEngine
from intheknow.mcp_server import build_server
from conftest import TODAY

SALES = "date,customer,offering,amount_toman\n2026-10-20,Hamid Moradi,Item A,140000\n2026-10-20,Mina Rahimi,Item B,250000\n2026-10-21,Hamid Moradi,Item A,170000\n"
SUPPLIERS = "supplier,product,contact_person,phone\nSample Roasters Co,Espresso beans,Sara Sample,+40700000001\nSample Bean Trading,Ethiopia,Sara Sample,+40700000001\n"


def call(s, name, args=None):
    r = asyncio.run(s.call_tool(name, args or {}))
    return r.content[0].text, r.structured_content, bool(r.is_error)


def cafe(tmp_path):
    e = ContextEngine(tmp_path / "c.db", TODAY)
    s = build_server(e)
    call(s, "setup_business", {"name": "Mosiho", "vertical": "cafe", "locale": "en", "currency": "USD", "country": "RO"})
    return e, s


def n(e, sql):
    return e.conn.execute(sql).fetchone()[0]


def test_generic_link_columns_are_recognized(tmp_path):
    e, s = cafe(tmp_path)
    text, view, _ = call(s, "add_data", {"filename": "sales.csv", "content": SALES})
    assert "offering → product.name" in text and "new field" not in text
    assert n(e, "SELECT COUNT(*) FROM v_transactions WHERE offering IS NOT NULL") == 3


def test_custom_table_updates_instead_of_duplicating(tmp_path):
    e, s = cafe(tmp_path)
    call(s, "change_schema", {"action": "create_entity", "spec": {"name": "supplier", "role": "custom", "label": {"en": ["Supplier", "Suppliers"]},
                                                                 "fields": [{"name": "name", "type": "text", "synonyms": ["supplier"]},
                                                                            {"name": "contact_person", "type": "text"}, {"name": "phone", "type": "phone"}]}})
    call(s, "add_data", {"filename": "suppliers.csv", "content": SUPPLIERS})
    call(s, "import_records", {"entity": "supplier", "source": "chat:assign", "rows": [
        {"name": "Sample Roasters Co", "contact_person": "John"}, {"name": "Sample Bean Trading", "contact_person": "Stefan"}]})
    assert n(e, "SELECT COUNT(*) FROM e_supplier") == 2
    assert n(e, "SELECT contact_person FROM e_supplier WHERE name='Sample Roasters Co'") == "John"
    # re-mapping the same file replaces its rows instead of adding more
    fid = n(e, "SELECT id FROM raw_files WHERE name='suppliers.csv'")
    call(s, "map_columns", {"file_id": fid, "entity": "supplier", "columns": {"supplier": "name", "contact_person": "contact_person", "phone": "phone", "product": "new"}})
    assert n(e, "SELECT COUNT(*) FROM e_supplier") == 2


def test_cleaning_a_messy_import_end_to_end(tmp_path):
    e, s = cafe(tmp_path)
    # recreate the broken state: a text column instead of a link, duplicate custom rows
    call(s, "change_schema", {"action": "add_field", "entity": "sale", "field": {"name": "offering_text", "type": "text"}})
    call(s, "add_data", {"filename": "sales.csv", "content": SALES.replace("offering", "offering_text")})
    call(s, "change_schema", {"action": "create_entity", "spec": {"name": "supplier", "role": "custom", "label": {"en": ["Supplier", "Suppliers"]},
                                                                 "fields": [{"name": "title", "type": "text"}, {"name": "score", "type": "text"}]}})
    e.conn.executescript("INSERT INTO e_supplier(title, score) VALUES ('Alborz', '8'), ('alborz', NULL), ('Carpathian', 'nine');")
    # 1. look
    text, view, _ = call(s, "browse_records", {"entity": "sale", "where": {"empty": ["product"]}})
    assert "| id |" in text or view["blocks"][1]["rows"]
    # 2. copy the text column into the product link (preview not needed: 3 rows) and drop the text column
    text, view, err = call(s, "edit_records", {"entity": "sale", "action": "update", "where": {"empty": ["product"]},
                                               "set": {"product": {"from_field": "offering_text"}}})
    assert not err and n(e, "SELECT COUNT(*) FROM v_transactions WHERE offering='Item A'") == 2
    undo_copy = view["blocks"][0]["items"][0]["undo"]
    text, view, err = call(s, "change_schema", {"action": "remove_field", "entity": "sale", "field": "offering_text"})
    assert "NOTHING CHANGED YET" in text                                       # has data -> preview first
    text, view, err = call(s, "change_schema", {"action": "remove_field", "entity": "sale", "field": "offering_text", "confirm": True})
    assert not err
    # 3. dedupe suppliers (preview, then confirm)
    assert "NOTHING CHANGED YET" in call(s, "edit_records", {"entity": "supplier", "action": "dedupe"})[0]
    call(s, "edit_records", {"entity": "supplier", "action": "dedupe", "confirm": True})
    assert n(e, "SELECT COUNT(*) FROM e_supplier") == 2 and n(e, "SELECT score FROM e_supplier WHERE title='Alborz'") == "8"
    # 4. convert a text column to numbers; unreadable values are reported, not invented
    text, view, err = call(s, "change_schema", {"action": "change_field", "entity": "supplier", "field": "score", "new_type": "number", "confirm": True})
    assert not err and "could not be converted" in text
    # 5. rename + relabel
    call(s, "change_schema", {"action": "rename_field", "entity": "supplier", "field": "title", "new_name": "name", "label": {"en": "Name", "ro": "Nume"}})
    assert n(e, "SELECT COUNT(*) FROM e_supplier WHERE name='Carpathian'") == 1
    # 6. delete a bad import by file id (preview, then confirm), then undo it
    fid = n(e, "SELECT id FROM raw_files WHERE name='sales.csv'")
    assert "NOTHING CHANGED YET" in call(s, "edit_records", {"entity": "sale", "action": "delete", "where": {"file_id": fid}})[0]
    text, view, _ = call(s, "edit_records", {"entity": "sale", "action": "delete", "where": {"file_id": fid}, "confirm": True})
    assert n(e, "SELECT COUNT(*) FROM e_sale") == 0
    call(s, "undo_change", {"change_id": view["blocks"][0]["items"][0]["undo"]})
    assert n(e, "SELECT COUNT(*) FROM e_sale") == 3
    # 7. undo the column removal and the copy: back to the original state, exactly
    rm = n(e, "SELECT id FROM schema_changes WHERE op='remove_field'")
    call(s, "undo_change", {"change_id": str(rm)})
    assert n(e, "SELECT COUNT(*) FROM e_sale WHERE offering_text='Item A'") == 2
    call(s, "undo_change", {"change_id": undo_copy})
    assert n(e, "SELECT COUNT(*) FROM e_sale WHERE product_id IS NOT NULL") == 0
    # 8. remove a whole table, then bring it back
    call(s, "change_schema", {"action": "remove_entity", "entity": "supplier", "confirm": True})
    assert "supplier" not in e.conn.execute("SELECT group_concat(name) FROM entity_types").fetchone()[0]
    rid = n(e, "SELECT id FROM schema_changes WHERE op='remove_entity'")
    call(s, "undo_change", {"change_id": str(rid)})
    assert n(e, "SELECT COUNT(*) FROM e_supplier") == 2


def test_safety_rules(tmp_path):
    e, s = cafe(tmp_path)
    call(s, "add_data", {"filename": "sales.csv", "content": SALES})
    assert call(s, "change_schema", {"action": "remove_field", "entity": "sale", "field": "amount", "confirm": True})[2]       # required field
    assert call(s, "edit_records", {"entity": "sale", "action": "update", "set": {"amount": "1"}})[2]                         # no filter
    assert call(s, "edit_records", {"entity": "customer", "action": "delete", "where": {"equals": {"name": "Hamid Moradi"}}, "confirm": True})[2]  # still has sales
    assert call(s, "edit_records", {"entity": "sale", "action": "update", "where": {"ids": [1]}, "set": {"amount": "abc"}})[2]  # unreadable value
    assert call(s, "browse_records", {"entity": "sale", "where": {"sql": "1=1; DROP TABLE e_sale"}})[2]                       # no raw SQL
    # merging two customers of a cafe moves their sales
    call(s, "capture", {"items": [{"type": "customer_info", "quote": "q", "customer": "H. Moradi"}]})
    call(s, "edit_records", {"entity": "customer", "action": "merge", "keep": "Hamid Moradi", "duplicate": "H. Moradi", "confirm": True})
    assert n(e, "SELECT COUNT(*) FROM v_parties") == 2


def test_failed_tool_changes_nothing(tmp_path):
    e, s = cafe(tmp_path)
    call(s, "add_data", {"filename": "sales.csv", "content": SALES})
    before = n(e, "SELECT COUNT(*) FROM e_sale")
    # quantity is written first, then the amount fails: the whole call must roll back, quantity included
    text, _, err = call(s, "edit_records", {"entity": "sale", "action": "update", "where": {"ids": [1, 2]}, "set": {"quantity": "2", "amount": "abc"}})
    assert err and n(e, "SELECT COUNT(*) FROM e_sale WHERE quantity IS NOT NULL") == 0 and n(e, "SELECT COUNT(*) FROM e_sale") == before
