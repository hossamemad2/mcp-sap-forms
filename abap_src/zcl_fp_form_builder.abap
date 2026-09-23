*&---------------------------------------------------------------*
*& Class ZCL_FP_FORM_BUILDER
*& Generic engine to create + activate an Adobe Interface (SFPI)
*& and Adobe Form (SFPF) pair from an input field list and an XDP
*& layout supplied as XSTRING. Deployed into package $TMP only --
*& this class is dev-client tooling, never transported itself.
*&---------------------------------------------------------------*
CLASS zcl_fp_form_builder DEFINITION PUBLIC FINAL CREATE PUBLIC.

  PUBLIC SECTION.

    TYPES:
      BEGIN OF ty_field,
        name     TYPE fpfield,
        typing   TYPE char4,      " 'TYPE' or 'LIKE'
        typename TYPE string,     " e.g. 'STRING', 'ZTRM_LG_ITEM_T'
        parent   TYPE fpfield,    " optional: name of parent field
        before   TYPE fpfield,    " optional: insert before this sibling
      END OF ty_field,
      ty_fields TYPE STANDARD TABLE OF ty_field WITH EMPTY KEY.

    CLASS-METHODS:
      interface_exists
        IMPORTING i_name          TYPE fpname
        RETURNING VALUE(r_exists) TYPE abap_bool,

      form_exists
        IMPORTING i_name          TYPE fpname
        RETURNING VALUE(r_exists) TYPE abap_bool,

      package_of_program
        IMPORTING i_progname        TYPE progname
        RETURNING VALUE(r_devclass) TYPE devclass.

    METHODS:
      create_interface
        IMPORTING
          i_name     TYPE fpname
          i_devclass TYPE devclass
          i_ordernum TYPE trkorr OPTIONAL
          it_fields  TYPE ty_fields
        RAISING
          cx_fp_api,

      create_form
        IMPORTING
          i_name           TYPE fpname
          i_interface_name TYPE fpname
          i_devclass       TYPE devclass
          i_ordernum       TYPE trkorr OPTIONAL
          i_xdp_layout     TYPE xstring
        RAISING
          cx_fp_api.

  PRIVATE SECTION.
    METHODS:
      build_context
        IMPORTING
          io_context TYPE REF TO if_fp_context
          it_fields  TYPE ty_fields
        RAISING
          cx_fp_api.

ENDCLASS.


CLASS zcl_fp_form_builder IMPLEMENTATION.

  METHOD interface_exists.
    SELECT SINGLE @abap_true FROM tadir
      INTO @r_exists
      WHERE pgmid = 'R3TR' AND object = 'SFPI' AND obj_name = @i_name.
  ENDMETHOD.

  METHOD form_exists.
    SELECT SINGLE @abap_true FROM tadir
      INTO @r_exists
      WHERE pgmid = 'R3TR' AND object = 'SFPF' AND obj_name = @i_name.
  ENDMETHOD.

  METHOD package_of_program.
    SELECT SINGLE devclass FROM tadir
      INTO @r_devclass
      WHERE pgmid = 'R3TR' AND object = 'PROG' AND obj_name = @i_progname.
  ENDMETHOD.

  METHOD create_interface.
    DATA: lo_interface TYPE REF TO if_fp_interface,
          lo_idata     TYPE REF TO if_fp_interface_data,
          lo_params    TYPE REF TO if_fp_parameters,
          lo_wb_intf   TYPE REF TO if_fp_wb_interface,
          lt_import    TYPE tfpiopar.

    LOOP AT it_fields INTO DATA(ls_field).
      APPEND VALUE #( name     = ls_field-name
                       typing   = ls_field-typing
                       typename = ls_field-typename ) TO lt_import.
    ENDLOOP.

    lo_interface = cl_fp_interface=>create( i_language = 'E' ).
    lo_idata     = lo_interface->get_interface_data( ).
    lo_params    = lo_idata->get_parameters( ).
    lo_params->set_import_parameters( i_import_parameters = lt_import ).

    lo_wb_intf = cl_fp_wb_interface=>create(
      i_devclass  = i_devclass
      i_interface = lo_interface
      i_name      = i_name
      i_ordernum  = i_ordernum ).
    lo_wb_intf->save( ).
    lo_wb_intf->activate( ).
    lo_wb_intf->free( ).
    COMMIT WORK AND WAIT.
  ENDMETHOD.

  METHOD create_form.
    DATA: lo_form      TYPE REF TO if_fp_form,
          lo_context   TYPE REF TO if_fp_context,
          lo_layout    TYPE REF TO if_fp_layout,
          lo_wb_form   TYPE REF TO if_fp_wb_form,
          lo_interface TYPE REF TO if_fp_interface,
          lo_idata     TYPE REF TO if_fp_interface_data,
          lo_params    TYPE REF TO if_fp_parameters,
          lt_import    TYPE tfpiopar,
          lt_fields    TYPE ty_fields.

    lo_interface = cl_fp_interface=>load( i_name = i_interface_name ).
    lo_idata     = lo_interface->get_interface_data( ).
    lo_params    = lo_idata->get_parameters( ).
    lt_import    = lo_params->get_import_parameters( ).

    LOOP AT lt_import INTO DATA(ls_import).
      APPEND VALUE #( name = ls_import-name typing = ls_import-typing
                       typename = ls_import-typename ) TO lt_fields.
    ENDLOOP.

    lo_form = cl_fp_form=>create( i_language = 'E' ).
    lo_form->set_interface_name( i_interface_name ).
    lo_context = lo_form->get_context( ).

    build_context( io_context = lo_context it_fields = lt_fields ).

    lo_layout = lo_form->get_layout( ).
    lo_layout->set_form_tech( if_fp_layout=>c_form_tech_xfa ).
    lo_layout->set_layout_type( if_fp_layout=>c_layout_type_standard ).
    lo_layout->set_layout_data(
      i_layout_data   = i_xdp_layout
      i_set_xliff_ids = abap_false ).

    lo_wb_form = cl_fp_wb_form=>create(
      i_devclass = i_devclass
      i_form     = lo_form
      i_name     = i_name
      i_ordernum = i_ordernum ).
    lo_wb_form->save( ).
    lo_wb_form->activate( ).
    lo_wb_form->free( ).
    COMMIT WORK AND WAIT.
  ENDMETHOD.

  METHOD build_context.
    DATA: lo_before TYPE REF TO if_fp_node,
          lo_parent TYPE REF TO if_fp_node,
          lo_data   TYPE REF TO if_fp_data.

    DATA: BEGIN OF ls_node_map,
            name TYPE fpfield,
            node TYPE REF TO if_fp_node,
          END OF ls_node_map.
    DATA lt_node_map LIKE STANDARD TABLE OF ls_node_map.

    LOOP AT it_fields INTO DATA(ls_field).
      CLEAR: lo_before, lo_parent.

      IF ls_field-parent IS NOT INITIAL.
        READ TABLE lt_node_map INTO ls_node_map
          WITH KEY name = ls_field-parent.
        IF sy-subrc = 0.
          lo_parent = ls_node_map-node.
        ENDIF.
      ENDIF.

      IF ls_field-before IS NOT INITIAL.
        READ TABLE lt_node_map INTO ls_node_map
          WITH KEY name = ls_field-before.
        IF sy-subrc = 0.
          lo_before = ls_node_map-node.
        ENDIF.
      ENDIF.

      lo_data = io_context->create_data(
        i_before = lo_before
        i_field  = CONV fpfield( ls_field-name )
        i_name   = CONV fpnodename( ls_field-name )
        i_parent = lo_parent ).

      APPEND VALUE #( name = ls_field-name node = lo_data ) TO lt_node_map.
    ENDLOOP.
  ENDMETHOD.

ENDCLASS.
