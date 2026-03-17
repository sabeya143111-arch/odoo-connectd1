import os
import json
import xmlrpc.client
from functools import lru_cache

import pandas as pd
import streamlit as st

# -------------------------------------------------------------------
# 1) Config / Secrets loading
# -------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def load_config():
    """
    Optional: local config.json se sirf field names lo.
    Secrets me company details honge.
    """
    cfg = {
        "model_field": "default_code",
        "template_model_field": "x_model_no",
        "variant_code_field": "default_code",
    }

    # Agar local config.json hai to usse override kar lo (optional)
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
            cfg.update(file_cfg)
        except Exception:
            pass

    return cfg


@st.cache_resource(show_spinner=False)
def load_companies_from_secrets():
    """
    Streamlit secrets se 3 Odoo systems + fields config load.
    Secrets.toml structure:

    [swag]
    name = "SWAG (Main)"
    url = "https://..."
    db = "..."
    user = "..."
    api_key = "..."

    [larouche]
    ...

    [different_clothes]
    ...

    [fields]
    model_field = "default_code"
    template_model_field = "x_model_no"
    variant_code_field = "default_code"
    """
    base_cfg = load_config()

    swag = dict(st.secrets["swag"])
    larouche = dict(st.secrets["larouche"])
    diffc = dict(st.secrets["different_clothes"])

    fields = st.secrets.get("fields", {})
    model_field = fields.get("model_field", base_cfg.get("model_field", "default_code"))
    template_model_field = fields.get(
        "template_model_field", base_cfg.get("template_model_field", "x_model_no")
    )
    variant_code_field = fields.get(
        "variant_code_field", base_cfg.get("variant_code_field", "default_code")
    )

    cfg = {
        "swag": swag,
        "larouche": larouche,
        "different_clothes": diffc,
        "model_field": model_field,
        "template_model_field": template_model_field,
        "variant_code_field": variant_code_field,
    }
    return cfg


# -------------------------------------------------------------------
# 2) Odoo connection helpers
# -------------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def connect_odoo(sys_key: str, conf: dict):
    """
    Connects to one Odoo instance via XML-RPC using API key as password.
    """
    url = conf["url"].rstrip("/")
    db = conf["db"]
    user = conf["user"]
    api_key = conf["api_key"]  # Odoo API key = password

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, user, api_key, {})
    if not uid:
        raise RuntimeError(f"Login failed for {sys_key} ({url})")

    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    return db, uid, api_key, models


def get_qty_for_models(sys_key: str, conf: dict, model_values, model_field: str):
    """
    Simple mode: bulk fetch qty_available for list of model_values from one Odoo.
    Works on product.product directly (single row per model).
    """
    if not model_values:
        return {}

    db, uid, pwd, models = connect_odoo(sys_key, conf)

    domain = [[model_field, "in", model_values]]
    products = models.execute_kw(
        db,
        uid,
        pwd,
        "product.product",
        "search_read",
        [domain],
        {
            "fields": ["id", model_field, "display_name", "qty_available"],
            "limit": 5000,
        },
    )

    result = {}
    for p in products:
        key = p.get(model_field)
        if key:
            result[key] = {
                "name": p.get("display_name", ""),
                "qty": float(p.get("qty_available", 0.0)),
            }
    return result


def get_template_and_variants(
    sys_key: str,
    conf: dict,
    template_model_value: str,
    template_model_field: str,
    variant_code_field: str,
):
    """
    Variant mode:
      1) Find product.template by template_model_field (e.g. x_model_no).
      2) Read all product.product variants under that template.
    Returns: dict with template and list of variant dicts.
    """
    db, uid, pwd, models = connect_odoo(sys_key, conf)

    tmpl_domain = [[template_model_field, "=", template_model_value]]
    templates = models.execute_kw(
        db,
        uid,
        pwd,
        "product.template",
        "search_read",
        [tmpl_domain],
        {"fields": ["id", "name", "product_variant_ids"], "limit": 1},
    )

    if not templates:
        return None

    tmpl = templates[0]
    variant_ids = tmpl.get("product_variant_ids") or []
    if not variant_ids:
        return {"template": tmpl, "variants": []}

    variant_fields = [
        "id",
        "display_name",
        "default_code",
        "qty_available",
        "attribute_value_ids",
    ]
    if variant_code_field not in variant_fields:
        variant_fields.append(variant_code_field)

    variants = models.execute_kw(
        db,
        uid,
        pwd,
        "product.product",
        "read",
        [variant_ids],
        {"fields": variant_fields},
    )

    # Read attribute values for readable combination
    attr_value_ids = set()
    for v in variants:
        for av in v.get("attribute_value_ids", []):
            attr_value_ids.add(av)
    attr_values_map = {}
    if attr_value_ids:
        attr_values = models.execute_kw(
            db,
            uid,
            pwd,
            "product.attribute.value",
            "read",
            [list(attr_value_ids)],
            {"fields": ["id", "name", "attribute_id"]},
        )
        for av in attr_values:
            attr_values_map[av["id"]] = av

    clean_variants = []
    for v in variants:
        av_ids = v.get("attribute_value_ids", [])
        attrs_text = []
        for av_id in av_ids:
            av = attr_values_map.get(av_id)
            if av:
                attr_name = av["attribute_id"][1] if av.get("attribute_id") else ""
                attrs_text.append(f"{attr_name}: {av.get('name', '')}")
        attrs_str = ", ".join(attrs_text)

        code_val = v.get(variant_code_field) or v.get("default_code") or ""
        clean_variants.append(
            {
                "id": v["id"],
                "code": code_val,
                "name": v.get("display_name", ""),
                "attrs": attrs_str,
                "qty": float(v.get("qty_available", 0.0)),
            }
        )

    return {"template": tmpl, "variants": clean_variants}


def build_variant_map_for_system(
    sys_key: str,
    conf: dict,
    model_values,
    template_model_field: str,
    variant_code_field: str,
):
    """
    For a list of template model values (e.g. model numbers),
    returns:
      - template_name_map: model_value -> template name
      - variant_map: (model_value, variant_code) -> variant info
    """
    template_name_map = {}
    variant_map = {}

    for m in model_values:
        data = get_template_and_variants(
            sys_key,
            conf,
            template_model_value=m,
            template_model_field=template_model_field,
            variant_code_field=variant_code_field,
        )
        if not data:
            continue

        tmpl = data["template"]
        template_name_map[m] = tmpl.get("name", "")
        for v in data["variants"]:
            key = (m, v["code"])
            variant_map[key] = {
                "name": v["name"],
                "attrs": v["attrs"],
                "qty": v["qty"],
            }

    return template_name_map, variant_map


# -------------------------------------------------------------------
# 3) Streamlit UI
# -------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="Odoo Multi-DB Stock Compare",
        page_icon="👕",
        layout="wide",
    )

    cfg = load_companies_from_secrets()
    swag = cfg["swag"]
    larouche = cfg["larouche"]
    diffc = cfg["different_clothes"]

    model_field_default = cfg.get("model_field", "default_code")
    template_model_field_default = cfg.get("template_model_field", "x_model_no")
    variant_code_field_default = cfg.get("variant_code_field", "default_code")

    # Sidebar
    st.sidebar.title("⚙️ Settings")
    st.sidebar.markdown("**Odoo Connections** (from secrets)")
    st.sidebar.write(f"✅ {swag['name']}")
    st.sidebar.write(f"✅ {larouche['name']}")
    st.sidebar.write(f"✅ {diffc['name']}")

    mode = st.sidebar.radio(
        "Result mode",
        ["Template total (simple)", "Variant wise (size/color)"],
    )

    if mode == "Template total (simple)":
        model_field = st.sidebar.text_input(
            "Model field on product.product",
            value=model_field_default,
            help="e.g. default_code, x_model_no (on product.product)",
        )
    else:
        template_model_field = st.sidebar.text_input(
            "Template model field (on product.template)",
            value=template_model_field_default,
            help="e.g. x_model_no (template level model code)",
        )
        variant_code_field = st.sidebar.text_input(
            "Variant code field (on product.product)",
            value=variant_code_field_default,
            help="e.g. default_code, x_sku (unique per variant)",
        )

    st.sidebar.info(
        "URLs, DB names, API keys Streamlit secrets se aa rahe hain. "
        "Repo public karne se pehle config.json me real secrets mat rakho."
    )

    # Main UI
    st.title("👕 3 Odoo Databases – Stock Comparison")
    st.caption(
        "SWAG, La Rouche aur Different Clothes me same model ka stock compare karo. "
        "Ab variant wise bhi dekh sakte ho."
    )

    col1, col2 = st.columns([2, 1])

    with col1:
        models_text = st.text_area(
            "Model numbers (har line me 1)",
            placeholder="MM0579\nMM0583\nMM0389",
            height=220,
        )

    with col2:
        st.markdown("**Kaise use kare**")
        if mode == "Template total (simple)":
            st.markdown(
                "- Upar model numbers paste karo (product.product ke field se).\n"
                "- Neeche **Compare Quantities** button dabao.\n"
                "- Table me teeno Odoo ka total stock per model aayega."
            )
        else:
            st.markdown(
                "- Upar template model numbers paste karo (product.template ka field).\n"
                "- Neeche **Compare Quantities** dabao.\n"
                "- Table me har model ke saare variants (size/color) alag rows me aayenge."
            )

        include_zero = st.checkbox(
            "Zero quantity wale rows bhi dikhana hai", value=True
        )

    models_list = [
        m.strip()
        for m in models_text.splitlines()
        if m.strip()
    ]

    if st.button("🔍 Compare Quantities", type="primary"):
        if not models_list:
            st.warning("Pehle kam se kam 1 model number daalo.")
            st.stop()

        if mode == "Template total (simple)":
            # Simple mode
            with st.spinner("Teeno Odoo se quantities nikal rahe hain..."):
                swag_map = get_qty_for_models("swag", swag, models_list, model_field)
                lrc_map = get_qty_for_models(
                    "larouche", larouche, models_list, model_field
                )
                diff_map = get_qty_for_models(
                    "different_clothes", diffc, models_list, model_field
                )

            rows = []
            for m in models_list:
                s = swag_map.get(m, {})
                l = lrc_map.get(m, {})
                d = diff_map.get(m, {})

                swag_qty = s.get("qty", 0.0)
                lrc_qty = l.get("qty", 0.0)
                diff_qty = d.get("qty", 0.0)

                if (
                    not include_zero
                    and (swag_qty == 0 and lrc_qty == 0 and diff_qty == 0)
                ):
                    continue

                name = s.get("name") or l.get("name") or d.get("name") or ""
                rows.append(
                    {
                        "Model": m,
                        "Product Name": name,
                        swag["name"]: swag_qty,
                        larouche["name"]: lrc_qty,
                        diffc["name"]: diff_qty,
                    }
                )

            if not rows:
                st.info("Koi data nahi mila (shayad sab zero ya model mismatch).")
                st.stop()

            df = pd.DataFrame(rows)

            st.subheader("📊 Quantity Comparison (Template total)")
            st.dataframe(
                df.style.format(
                    {
                        swag["name"]: "{:.2f}",
                        larouche["name"]: "{:.2f}",
                        diffc["name"]: "{:.2f}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        else:
            # Variant-wise mode
            with st.spinner("Teeno Odoo me variants nikal rahe hain..."):
                swag_tmpl_names, swag_variants = build_variant_map_for_system(
                    "swag",
                    swag,
                    models_list,
                    template_model_field,
                    variant_code_field,
                )
                lrc_tmpl_names, lrc_variants = build_variant_map_for_system(
                    "larouche",
                    larouche,
                    models_list,
                    template_model_field,
                    variant_code_field,
                )
                diff_tmpl_names, diff_variants = build_variant_map_for_system(
                    "different_clothes",
                    diffc,
                    models_list,
                    template_model_field,
                    variant_code_field,
                )

            all_keys = set(swag_variants.keys()) | set(lrc_variants.keys()) | set(
                diff_variants.keys()
            )

            rows = []
            for model_val, vcode in sorted(all_keys):
                s = swag_variants.get((model_val, vcode), {})
                l = lrc_variants.get((model_val, vcode), {})
                d = diff_variants.get((model_val, vcode), {})

                swag_qty = s.get("qty", 0.0)
                lrc_qty = l.get("qty", 0.0)
                diff_qty = d.get("qty", 0.0)

                if (
                    not include_zero
                    and (swag_qty == 0 and lrc_qty == 0 and diff_qty == 0)
                ):
                    continue

                tmpl_name = (
                    swag_tmpl_names.get(model_val)
                    or lrc_tmpl_names.get(model_val)
                    or diff_tmpl_names.get(model_val)
                    or ""
                )
                name = s.get("name") or l.get("name") or d.get("name") or ""
                attrs = s.get("attrs") or l.get("attrs") or d.get("attrs") or ""

                rows.append(
                    {
                        "Model": model_val,
                        "Template Name": tmpl_name,
                        "Variant Code": vcode,
                        "Variant Name": name,
                        "Attributes": attrs,
                        swag["name"]: swag_qty,
                        larouche["name"]: lrc_qty,
                        diffc["name"]: diff_qty,
                    }
                )

            if not rows:
                st.info(
                    "Koi variant data nahi mila (shayad model galat hai ya teeno me variants nahi bane)."
                )
                st.stop()

            df = pd.DataFrame(rows)

            st.subheader("📊 Quantity Comparison (Variant wise)")
            st.dataframe(
                df.style.format(
                    {
                        swag["name"]: "{:.2f}",
                        larouche["name"]: "{:.2f}",
                        diffc["name"]: "{:.2f}",
                    }
                ),
                use_container_width=True,
                hide_index=True,
            )

        # Download
        csv = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ Download as CSV",
            csv,
            file_name="odoo_multi_db_qty_compare.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
