--
-- PostgreSQL database dump
--

\restrict UpwZLD2OqrfRy5KxzTfwJmELNliEtAbA7d1XHfKbgRYlj5lj90NBMFLIHKI5q0p

-- Dumped from database version 16.11
-- Dumped by pg_dump version 18.6

-- Started on 2026-11-06 10:26:02

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- TOC entry 495 (class 2615 OID 49921922)
-- Name: master_thach; Type: SCHEMA; Schema: -; Owner: dmms_dev
--

CREATE SCHEMA master_thach;


ALTER SCHEMA master_thach OWNER TO dmms_dev;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- TOC entry 1821 (class 1259 OID 49926592)
-- Name: district; Type: TABLE; Schema: master_thach; Owner: dmms_dev
--

CREATE TABLE master_thach.district (
    district_id character varying(10) NOT NULL,
    district_name character varying(100),
    province_id character varying(10)
);


ALTER TABLE master_thach.district OWNER TO dmms_dev;

--
-- TOC entry 1822 (class 1259 OID 49926699)
-- Name: province; Type: TABLE; Schema: master_thach; Owner: dmms_dev
--

CREATE TABLE master_thach.province (
    province_id character varying(10) NOT NULL,
    province_name character varying(100)
);


ALTER TABLE master_thach.province OWNER TO dmms_dev;

--
-- TOC entry 1823 (class 1259 OID 49926969)
-- Name: subdistrict; Type: TABLE; Schema: master_thach; Owner: dmms_dev
--

CREATE TABLE master_thach.subdistrict (
    subdistrict_id character varying(10) NOT NULL,
    subdistrict_name character varying(100),
    postcode character varying(5),
    district_id character varying(10),
    CONSTRAINT chk_subdistrict_postcode CHECK (((postcode)::text ~ '^[0-9]{5}$'::text))
);


ALTER TABLE master_thach.subdistrict OWNER TO dmms_dev;

--
-- TOC entry 8013 (class 0 OID 49926592)
-- Dependencies: 1821
-- Data for Name: district; Type: TABLE DATA; Schema: master_thach; Owner: dmms_dev
--

COPY master_thach.district (district_id, district_name, province_id) FROM stdin;
1001	พระนคร	10
1101	เมืองสมุทรปราการ	11
\.


--
-- TOC entry 8014 (class 0 OID 49926699)
-- Dependencies: 1822
-- Data for Name: province; Type: TABLE DATA; Schema: master_thach; Owner: dmms_dev
--

COPY master_thach.province (province_id, province_name) FROM stdin;
10	กรุงเทพมหานคร
11	สมุทรปราการ
\.


--
-- TOC entry 8015 (class 0 OID 49926969)
-- Dependencies: 1823
-- Data for Name: subdistrict; Type: TABLE DATA; Schema: master_thach; Owner: dmms_dev
--

COPY master_thach.subdistrict (subdistrict_id, subdistrict_name, postcode, district_id) FROM stdin;
100101	พระบรมมหาราชวัง	10200	1001
100102	วังบูรพาภิรมย์	10200	1001
110101	ปากน้ำ	10270	1101
\.


--
-- TOC entry 7856 (class 2606 OID 49928532)
-- Name: district district_pkey; Type: CONSTRAINT; Schema: master_thach; Owner: dmms_dev
--

ALTER TABLE ONLY master_thach.district
    ADD CONSTRAINT district_pkey PRIMARY KEY (district_id);


--
-- TOC entry 7859 (class 2606 OID 49928529)
-- Name: province province_pkey; Type: CONSTRAINT; Schema: master_thach; Owner: dmms_dev
--

ALTER TABLE ONLY master_thach.province
    ADD CONSTRAINT province_pkey PRIMARY KEY (province_id);


--
-- TOC entry 7862 (class 2606 OID 49928534)
-- Name: subdistrict subdistrict_pkey; Type: CONSTRAINT; Schema: master_thach; Owner: dmms_dev
--

ALTER TABLE ONLY master_thach.subdistrict
    ADD CONSTRAINT subdistrict_pkey PRIMARY KEY (subdistrict_id);


--
-- TOC entry 7857 (class 1259 OID 49928550)
-- Name: idx_district_province_id; Type: INDEX; Schema: master_thach; Owner: dmms_dev
--

CREATE INDEX idx_district_province_id ON master_thach.district USING btree (province_id);


--
-- TOC entry 7860 (class 1259 OID 49928551)
-- Name: idx_subdistrict_district_id; Type: INDEX; Schema: master_thach; Owner: dmms_dev
--

CREATE INDEX idx_subdistrict_district_id ON master_thach.subdistrict USING btree (district_id);


--
-- TOC entry 7863 (class 2606 OID 49928535)
-- Name: district fk_district_province; Type: FK CONSTRAINT; Schema: master_thach; Owner: dmms_dev
--

ALTER TABLE ONLY master_thach.district
    ADD CONSTRAINT fk_district_province FOREIGN KEY (province_id) REFERENCES master_thach.province(province_id) ON DELETE RESTRICT;


--
-- TOC entry 7864 (class 2606 OID 49928540)
-- Name: subdistrict fk_subdistrict_district; Type: FK CONSTRAINT; Schema: master_thach; Owner: dmms_dev
--

ALTER TABLE ONLY master_thach.subdistrict
    ADD CONSTRAINT fk_subdistrict_district FOREIGN KEY (district_id) REFERENCES master_thach.district(district_id) ON DELETE RESTRICT;


-- Completed on 2026-11-06 10:26:08

--
-- PostgreSQL database dump complete
--

\unrestrict UpwZLD2OqrfRy5KxzTfwJmELNliEtAbA7d1XHfKbgRYlj5lj90NBMFLIHKI5q0p

